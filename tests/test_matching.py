"""Tests for the deterministic name-match baseline.

Bands rather than exact floats: the weights in `matching` are a tuning
decision that will be revisited as real PAN-name traffic arrives, and these
tests exist to pin the *ordering* of the cases (benign difference beats
substituted name) plus the two hard guarantees — symmetry, and never raising.
Where a number is a judgement call the assertion is a bound, and the score
observed at the time of writing is noted alongside it.
"""

from __future__ import annotations

import pytest

from savings_flow.agents.a01_id_verification.contract import (
    REFERRAL_FLOOR,
    VERIFIED_THRESHOLD,
)
from savings_flow.agents.a01_id_verification.matching import (
    FACTORS,
    MatchResult,
    normalize_name,
    score_names,
    tokenize_name,
)


def test_exact_match_scores_one() -> None:
    result = score_names("Rajesh Kumar Sharma", "Rajesh Kumar Sharma")
    assert result.score == 1.0
    assert "exact_match" in result.factors
    assert result.normalized_input == "RAJESH KUMAR SHARMA"
    assert result.normalized_registered == "RAJESH KUMAR SHARMA"


def test_case_punctuation_and_whitespace_only_difference() -> None:
    result = score_names("  rajesh   kumar,  sharma ", "RAJESH KUMAR SHARMA")
    assert result.score == 1.0
    assert "case_or_punctuation_only" in result.factors
    assert "exact_match" not in result.factors


def test_initials_versus_expanded_given_names() -> None:
    # Observed 0.930: a PAN record holding initials is the single most common
    # benign mismatch, so it must clear the verification threshold on its own.
    result = score_names("R. K. Sharma", "Rajesh Kumar Sharma")
    assert result.score >= VERIFIED_THRESHOLD
    assert result.score <= 0.97
    assert "initials_expanded" in result.factors


def test_run_together_initials_match_spaced_initials() -> None:
    assert (
        score_names("RK Sharma", "Rajesh Kumar Sharma").score
        == score_names("R. K. Sharma", "Rajesh Kumar Sharma").score
    )
    assert tokenize_name("RK Sharma") == ["R", "K", "SHARMA"]
    # A leading vowel-bearing token is a name, not a cluster of initials.
    assert tokenize_name("Joy Sharma") == ["JOY", "SHARMA"]


def test_surname_first_ordering_costs_little() -> None:
    # Observed 0.980: word order carries no identity information.
    result = score_names("Sharma Rajesh Kumar", "Rajesh Kumar Sharma")
    assert result.score >= 0.95
    assert result.score < 1.0
    assert "token_order_differs" in result.factors


def test_honorific_on_one_side_is_stripped() -> None:
    result = score_names("Dr. Rajesh Sharma", "Rajesh Sharma")
    assert result.score >= VERIFIED_THRESHOLD
    assert "honorific_stripped" in result.factors
    assert normalize_name("Late Shri Rajesh Sharma") == "RAJESH SHARMA"


def test_honorific_only_input_keeps_a_token() -> None:
    # Bad data, not an empty name: stripping must never consume everything.
    assert normalize_name("Late") == "LATE"


def test_diacritics_are_folded() -> None:
    result = score_names("Rájesh Śharma", "Rajesh Sharma")
    assert result.score >= VERIFIED_THRESHOLD
    assert "diacritics_folded" in result.factors
    assert result.normalized_input == "RAJESH SHARMA"


def test_missing_middle_name_is_moderately_high() -> None:
    # Observed 0.825: high enough to be referral-grade, deliberately short of
    # auto-verify because a dropped middle name is also how a mismatch looks.
    result = score_names("Rajesh Sharma", "Rajesh Kumar Sharma")
    assert REFERRAL_FLOOR <= result.score < VERIFIED_THRESHOLD
    assert result.score >= 0.75
    assert "middle_name_missing" in result.factors


@pytest.mark.parametrize(
    ("left", "right", "low", "high"),
    [
        ("Lakshmi Devi", "Laxmi Devi", 0.45, 0.60),
        ("Mohammed Ali Khan", "Muhammad Ali Khan", 0.60, 0.75),
    ],
)
def test_transliteration_variants_are_only_mediocre(
    left: str, right: str, low: float, high: float
) -> None:
    # This is the gap the Gemini adjudicator exists to close. No string metric
    # separates these from genuinely different names: SUNIL/SUNITA scores
    # *higher* than LAKSHMI/LAXMI on JaroWinkler and on the indel ratio alike,
    # so raising the deterministic floor far enough to admit real
    # transliterations would also admit real strangers. The baseline therefore
    # scores them below verification on purpose and the model adjudicates with
    # knowledge of Indian transliteration that no edit distance encodes.
    result = score_names(left, right)
    assert low <= result.score <= high
    assert result.score < VERIFIED_THRESHOLD


def test_different_given_name_same_surname_is_low() -> None:
    # Observed 0.391 — a substituted given name is a different person.
    result = score_names("Suresh Sharma", "Rajesh Sharma")
    assert result.score <= 0.55
    assert result.score < REFERRAL_FLOOR
    assert "given_name_differs" in result.factors


def test_different_surname_same_given_name_is_low() -> None:
    result = score_names("Rajesh Verma", "Rajesh Sharma")
    assert result.score <= 0.55
    assert "surname_differs" in result.factors


def test_unrelated_names_are_very_low() -> None:
    result = score_names("Priya Venkatesan", "Rajesh Kumar Sharma")
    assert result.score <= 0.10
    assert "no_common_tokens" in result.factors


def test_initial_that_matches_nothing_is_flagged() -> None:
    result = score_names("A K Sharma", "Rajesh Kumar Sharma")
    assert result.score < REFERRAL_FLOOR
    assert "initial_mismatch" in result.factors


def test_near_identical_spellings_still_match() -> None:
    # Inflected endings are used interchangeably in Indian records.
    result = score_names("Krishnan Nair", "Krishna Nair")
    assert result.score >= VERIFIED_THRESHOLD
    assert "spelling_variant_matched" in result.factors


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Rajesh Kumar Sharma", "Rajesh Kumar Sharma"),
        ("R. K. Sharma", "Rajesh Kumar Sharma"),
        ("Sharma Rajesh Kumar", "Rajesh Kumar Sharma"),
        ("Rajesh Sharma", "Rajesh Kumar Sharma"),
        ("Suresh Sharma", "Rajesh Sharma"),
        ("Lakshmi Devi", "Laxmi Devi"),
        ("Priya Venkatesan", "Rajesh Kumar Sharma"),
        ("Rajesh", "Rajesh Kumar Sharma"),
        ("A K Sharma", "Rajesh Kumar Sharma"),
        ("", "Rajesh Sharma"),
    ],
)
def test_score_is_symmetric(left: str, right: str) -> None:
    forward = score_names(left, right)
    backward = score_names(right, left)
    assert forward.score == backward.score
    assert forward.factors == backward.factors
    assert forward.normalized_input == backward.normalized_registered


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "\t\n", "!!!", "12345", "---", "​", None, 12345, [], {"a": 1}],
)
def test_unusable_input_scores_zero_without_raising(bad: object) -> None:
    for result in (
        score_names(bad, "Rajesh Sharma"),  # type: ignore[arg-type]
        score_names("Rajesh Sharma", bad),  # type: ignore[arg-type]
        score_names(bad, bad),  # type: ignore[arg-type]
    ):
        assert isinstance(result, MatchResult)
        assert result.score == 0.0
        assert "no_common_tokens" in result.factors


def test_scores_stay_in_range_and_factors_are_known_codes() -> None:
    samples = [
        ("Rajesh Kumar Sharma", "Rajesh Kumar Sharma"),
        ("Dr Rájesh K Sharma", "Sharma Rajesh Kumar"),
        ("Laxmi", "Lakshmi Devi Iyer"),
        ("Mohammed Abdul Rahman Khan", "M A R Khan"),
        ("Sharma", "Rajesh Kumar Sharma"),
        ("!!!", "Rajesh Sharma"),
    ]
    for left, right in samples:
        result = score_names(left, right)
        assert 0.0 <= result.score <= 1.0
        assert result.factors, f"no factors explained for {left!r} vs {right!r}"
        assert set(result.factors) <= FACTORS
