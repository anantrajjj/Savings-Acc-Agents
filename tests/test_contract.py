"""Contract-enforcement tests for A01 — Identity Verification Agent.

Two things are under test here, and only these two: the Pydantic models that
*are* the platform contract, and the `assert_matches_contract` guardrail that
every other A01 suite leans on. Nothing agent-, service- or registry-shaped is
imported, so this file stands alone while those modules are still in flight.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from conftest import (
    EXPLAIN_LIST_KEYS,
    OPTIONAL_TOP_LEVEL_KEYS,
    REQUIRED_EXPLAIN_KEYS,
    REQUIRED_TOP_LEVEL_KEYS,
    assert_matches_contract,
)

try:
    from savings_flow.agents.a01_id_verification.contract import (
        REFERRAL_FLOOR,
        VERIFIED_THRESHOLD,
        A01Input,
        A01Output,
        Explain,
    )
except ModuleNotFoundError:
    # The A01 package __init__ eagerly imports the agent, which pulls in sibling
    # modules written in parallel with this suite. contract.py has no
    # intra-package imports of its own, so load it straight from its file: the
    # contract guardrail must stay green even when the rest of A01 is mid-flight.
    _CONTRACT_PATH = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "savings_flow"
        / "agents"
        / "a01_id_verification"
        / "contract.py"
    )
    _spec = importlib.util.spec_from_file_location("_a01_contract", _CONTRACT_PATH)
    assert _spec is not None and _spec.loader is not None, (
        f"could not load the A01 contract module from {_CONTRACT_PATH}"
    )
    _contract = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_contract)

    REFERRAL_FLOOR = _contract.REFERRAL_FLOOR
    VERIFIED_THRESHOLD = _contract.VERIFIED_THRESHOLD
    A01Input = _contract.A01Input
    A01Output = _contract.A01Output
    Explain = _contract.Explain

VALID_PAN = "ABCPD1234E"


def _explain(**overrides: Any) -> Explain:
    defaults: dict[str, Any] = {
        "reasoning_summary": "PAN found in registry; name matched.",
        "evidence_refs": ["pan-registry:ABCPD1234E"],
        "policy_citations": ["KYC-A01-1"],
        "confidence": 0.9,
        "deciding_factors": ["name_match_score"],
    }
    return Explain(**{**defaults, **overrides})


# --- A01Input ---------------------------------------------------------------


def test_input_ignores_unknown_keys() -> None:
    # The workflow around A01 grows; upstream agents will pass extra context.
    parsed = A01Input(pan=VALID_PAN, fullName="Asha Menon", correlationId="c-1")

    assert parsed.model_dump() == {"pan": VALID_PAN, "fullName": "Asha Menon"}
    assert not hasattr(parsed, "correlationId")


def test_input_strips_surrounding_whitespace() -> None:
    parsed = A01Input(pan=f"  {VALID_PAN}\t", fullName="  Asha Menon \n")

    assert parsed.pan == VALID_PAN
    assert parsed.fullName == "Asha Menon"


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"pan": "", "fullName": "Asha Menon"}, id="pan-empty"),
        pytest.param({"pan": "   ", "fullName": "Asha Menon"}, id="pan-whitespace"),
        pytest.param({"pan": VALID_PAN, "fullName": ""}, id="fullName-empty"),
        pytest.param({"pan": VALID_PAN, "fullName": "  "}, id="fullName-whitespace"),
        pytest.param({"fullName": "Asha Menon"}, id="pan-missing"),
        pytest.param({"pan": VALID_PAN}, id="fullName-missing"),
        pytest.param({}, id="both-missing"),
    ],
)
def test_input_rejects_blank_or_missing_fields(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        A01Input(**kwargs)


# --- Explain ----------------------------------------------------------------


def test_explain_rejects_unknown_sub_keys() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        Explain(
            reasoning_summary="ok",
            evidence_refs=[],
            policy_citations=[],
            confidence=0.5,
            deciding_factors=[],
            internal_debug="leaked",
        )


@pytest.mark.parametrize("confidence", [-0.01, -1.0, 1.01, 2.0])
def test_explain_rejects_out_of_range_confidence(confidence: float) -> None:
    with pytest.raises(ValidationError):
        _explain(confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_explain_accepts_boundary_confidence(confidence: float) -> None:
    assert _explain(confidence=confidence).confidence == confidence


def test_explain_list_fields_default_empty() -> None:
    explain = Explain(reasoning_summary="ok", confidence=0.5)

    for key in EXPLAIN_LIST_KEYS:
        assert getattr(explain, key) == []


# --- A01Output --------------------------------------------------------------


def test_output_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden|Extra inputs"):
        A01Output(
            verified=True,
            nameMatchScore=0.9,
            explain=_explain(),
            riskBand="LOW",
        )


@pytest.mark.parametrize("score", [-0.01, -1.0, 1.01, 1.5])
def test_output_rejects_out_of_range_score(score: float) -> None:
    with pytest.raises(ValidationError):
        A01Output(verified=False, nameMatchScore=score, explain=_explain())


def test_to_payload_omits_registered_name_when_none() -> None:
    payload = A01Output(
        verified=False, nameMatchScore=0.1, explain=_explain()
    ).to_payload()

    assert "registeredName" not in payload
    assert set(payload) == set(REQUIRED_TOP_LEVEL_KEYS)


def test_to_payload_includes_registered_name_when_set() -> None:
    payload = A01Output(
        verified=True,
        nameMatchScore=0.99,
        registeredName="ASHA MENON",
        explain=_explain(),
    ).to_payload()

    assert payload["registeredName"] == "ASHA MENON"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.876543, 0.877),
        (0.6666666, 0.667),
        (0.1234, 0.123),
        (1.0, 1.0),
        (0.0, 0.0),
    ],
)
def test_to_payload_rounds_score_to_three_decimals(raw: float, expected: float) -> None:
    payload = A01Output(
        verified=raw >= VERIFIED_THRESHOLD, nameMatchScore=raw, explain=_explain()
    ).to_payload()

    assert payload["nameMatchScore"] == expected


# --- Guardrail proof: the checker must actually catch violations -------------
# A guardrail nobody tested is not a guardrail. Each case mutates a known-good
# payload in one way and asserts the failure message names the offending key.

Mutator = Callable[[MutableMapping[str, Any]], None]


def _drop_score(payload: MutableMapping[str, Any]) -> None:
    del payload["nameMatchScore"]


def _add_extra_key(payload: MutableMapping[str, Any]) -> None:
    payload["riskBand"] = "LOW"


def _verified_as_string(payload: MutableMapping[str, Any]) -> None:
    payload["verified"] = "true"


def _verified_as_int(payload: MutableMapping[str, Any]) -> None:
    payload["verified"] = 1


def _score_as_bool(payload: MutableMapping[str, Any]) -> None:
    payload["nameMatchScore"] = True


def _score_out_of_range(payload: MutableMapping[str, Any]) -> None:
    payload["nameMatchScore"] = 1.5


def _score_as_string(payload: MutableMapping[str, Any]) -> None:
    payload["nameMatchScore"] = "0.9"


def _drop_deciding_factors(payload: MutableMapping[str, Any]) -> None:
    del payload["explain"]["deciding_factors"]


def _extra_explain_key(payload: MutableMapping[str, Any]) -> None:
    payload["explain"]["internal_debug"] = "leaked"


def _explain_not_an_object(payload: MutableMapping[str, Any]) -> None:
    payload["explain"] = "PAN matched"


def _evidence_ref_int(payload: MutableMapping[str, Any]) -> None:
    payload["explain"]["evidence_refs"] = [7]


def _policy_citations_not_a_list(payload: MutableMapping[str, Any]) -> None:
    payload["explain"]["policy_citations"] = "KYC-A01-1"


def _confidence_as_string(payload: MutableMapping[str, Any]) -> None:
    payload["explain"]["confidence"] = "high"


def _confidence_out_of_range(payload: MutableMapping[str, Any]) -> None:
    payload["explain"]["confidence"] = 1.2


def _empty_reasoning_summary(payload: MutableMapping[str, Any]) -> None:
    payload["explain"]["reasoning_summary"] = ""


def _null_registered_name(payload: MutableMapping[str, Any]) -> None:
    # `None` must be omitted, never serialised as JSON null.
    payload["registeredName"] = None


def _empty_registered_name(payload: MutableMapping[str, Any]) -> None:
    payload["registeredName"] = "   "


VIOLATIONS: list[tuple[str, Mutator, str]] = [
    ("missing-required-key", _drop_score, "missing required key"),
    ("unexpected-top-level-key", _add_extra_key, "unexpected key"),
    ("verified-as-string", _verified_as_string, "verified: expected JSON boolean"),
    ("verified-as-int", _verified_as_int, "verified: expected JSON boolean"),
    ("score-as-bool", _score_as_bool, "nameMatchScore: expected JSON number, got bool"),
    ("score-out-of-range", _score_out_of_range, "nameMatchScore: expected a number"),
    ("score-as-string", _score_as_string, "nameMatchScore: expected JSON number"),
    ("explain-missing-key", _drop_deciding_factors, "explain: missing required key"),
    ("explain-extra-key", _extra_explain_key, "explain: unexpected key"),
    ("explain-not-object", _explain_not_an_object, "explain: expected JSON object"),
    ("evidence-ref-int", _evidence_ref_int, "explain.evidence_refs[0]"),
    (
        "policy-citations-not-list",
        _policy_citations_not_a_list,
        "explain.policy_citations: expected JSON array",
    ),
    (
        "confidence-as-string",
        _confidence_as_string,
        "explain.confidence: expected JSON number",
    ),
    (
        "confidence-out-of-range",
        _confidence_out_of_range,
        "explain.confidence: expected a number",
    ),
    (
        "empty-reasoning-summary",
        _empty_reasoning_summary,
        "explain.reasoning_summary: expected a non-empty string",
    ),
    ("registered-name-null", _null_registered_name, "registeredName: expected JSON string"),
    (
        "registered-name-blank",
        _empty_registered_name,
        "registeredName: expected a non-empty string",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "expected_fragment"),
    [pytest.param(mutate, fragment, id=case) for case, mutate, fragment in VIOLATIONS],
)
def test_checker_catches_violation(
    sample_output: dict[str, Any],
    assert_matches_contract: Callable[[dict[str, Any]], None],
    mutate: Mutator,
    expected_fragment: str,
) -> None:
    mutate(sample_output)

    with pytest.raises(AssertionError) as excinfo:
        assert_matches_contract(sample_output)

    assert expected_fragment in str(excinfo.value), (
        f"message did not name the problem: {excinfo.value}"
    )


@pytest.mark.parametrize("payload", ["not a dict", ["verified"], None, 3])
def test_checker_rejects_non_object_payload(payload: Any) -> None:
    with pytest.raises(AssertionError, match="expected JSON object"):
        assert_matches_contract(payload)


def test_checker_accepts_the_sample(
    sample_output: dict[str, Any],
    assert_matches_contract: Callable[[dict[str, Any]], None],
) -> None:
    assert_matches_contract(sample_output)


# --- Every real payload the models can produce must satisfy the checker -----

OUTPUT_CASES: list[tuple[str, A01Output]] = [
    (
        "verified-with-name",
        A01Output(
            verified=True,
            nameMatchScore=0.97,
            registeredName="ASHA MENON",
            explain=_explain(),
        ),
    ),
    (
        "verified-without-name",
        A01Output(verified=True, nameMatchScore=0.9123456, explain=_explain()),
    ),
    (
        "referral-band",
        A01Output(
            verified=False,
            nameMatchScore=(REFERRAL_FLOOR + VERIFIED_THRESHOLD) / 2,
            registeredName="A MENON",
            explain=_explain(confidence=0.5, deciding_factors=["partial_name_match"]),
        ),
    ),
    (
        "unverified-no-name",
        A01Output(
            verified=False,
            nameMatchScore=0.0,
            explain=_explain(confidence=0.0, evidence_refs=[], policy_citations=[]),
        ),
    ),
    (
        "boundary-score-one",
        A01Output(
            verified=True,
            nameMatchScore=1.0,
            registeredName="ASHA MENON",
            explain=_explain(confidence=1.0),
        ),
    ),
    (
        "boundary-score-zero",
        A01Output(verified=False, nameMatchScore=0.0, explain=_explain(confidence=0.0)),
    ),
]


@pytest.mark.parametrize(
    "model",
    [pytest.param(model, id=case) for case, model in OUTPUT_CASES],
)
def test_to_payload_matches_contract(
    model: A01Output,
    assert_matches_contract: Callable[[dict[str, Any]], None],
    assert_json_round_trip: Callable[[dict[str, Any]], None],
) -> None:
    payload = model.to_payload()

    assert_matches_contract(payload)
    # The platform sees JSON, not Python objects.
    assert_json_round_trip(payload)


# --- Coherence of the contract's own constants ------------------------------


def test_thresholds_are_coherent() -> None:
    assert 0 < REFERRAL_FLOOR, "REFERRAL_FLOOR must be above 0"
    assert REFERRAL_FLOOR < VERIFIED_THRESHOLD, (
        "referral band would be empty or inverted: "
        f"REFERRAL_FLOOR={REFERRAL_FLOOR} VERIFIED_THRESHOLD={VERIFIED_THRESHOLD}"
    )
    assert VERIFIED_THRESHOLD <= 1, "VERIFIED_THRESHOLD must be reachable (<= 1)"


def test_schema_fixture_agrees_with_checker(
    platform_output_schema: dict[str, Any],
) -> None:
    # The hand-written schema is documentation; this keeps it honest.
    properties = platform_output_schema["properties"]

    assert platform_output_schema["required"] == list(REQUIRED_TOP_LEVEL_KEYS)
    assert set(properties) == set(REQUIRED_TOP_LEVEL_KEYS) | set(
        OPTIONAL_TOP_LEVEL_KEYS
    )
    assert platform_output_schema["additionalProperties"] is False

    explain_schema = properties["explain"]
    assert explain_schema["required"] == list(REQUIRED_EXPLAIN_KEYS)
    assert set(explain_schema["properties"]) == set(REQUIRED_EXPLAIN_KEYS)
    assert explain_schema["additionalProperties"] is False
    assert set(A01Output.model_fields) == set(REQUIRED_TOP_LEVEL_KEYS) | set(
        OPTIONAL_TOP_LEVEL_KEYS
    )
    assert set(Explain.model_fields) == set(REQUIRED_EXPLAIN_KEYS)
