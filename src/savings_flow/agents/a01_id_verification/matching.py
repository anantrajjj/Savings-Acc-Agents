"""Deterministic name matching for Indian personal names.

A01 compares the name a customer typed against the name registered against
their PAN. The two rarely agree character-for-character: PAN records carry
initials where the customer spells names out, drop or keep middle names, and
order surname-first; customers add honorifics and diacritics. This module
turns those routine, *benign* differences into a small cost and genuine
disagreements — a substituted given name or surname — into a large one.

A Gemini adjudicator sits on top of this score and A01 falls back to it
whenever the model is unavailable, so the baseline must stand alone and must
never raise. Transliteration variants (Lakshmi/Laxmi, Mohammed/Muhammad) are
the known blind spot: no edit-distance metric separates them from genuinely
different names (Sunil/Sunita scores *higher* than Lakshmi/Laxmi on every
string metric we measured), so they are deliberately left mediocre here for
the adjudicator to resolve with knowledge this module cannot have.

Scoring
-------
Two positive signals summing to 1.0, then penalties:

* ``0.70 x token_core`` — order-insensitive, initial-aware coverage of one
  token list by the other. Weighted highest because token identity is what
  actually establishes a person; word order does not. Internally it blends
  ``0.75`` of the coverage measured against the *shorter* name (so an omitted
  middle name is cheap) with ``0.25`` measured against the *longer* one (so a
  lone matching token cannot carry a whole name).
* ``0.30 x string_similarity`` — ``rapidfuzz.fuzz.token_sort_ratio`` over the
  initial-aligned names. Catches spelling drift that token equality misses,
  and is deliberately the minority signal because it punishes legitimate
  initials and reorderings that ``token_core`` correctly forgives.
* ``-0.35 x conflict`` — scaled by how *dissimilar* the contradicting tokens
  are, so ``Lakshmi``/``Laxmi`` costs a fraction of what ``Rajesh``/``Suresh``
  costs. A conflict is a token unmatched on *both* sides: a substitution
  rather than an omission, which is the sharpest fraud signal available here.
* ``-0.06 per extra token`` (capped at ``0.18``) — one-sided omissions.
* ``-0.02`` for word-order differences, which "cost little" by design.

An exact normalized match therefore scores 1.0, and the score is symmetric in
its two arguments (``normalized_input``/``normalized_registered`` swap, but
``score`` and ``factors`` do not).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

# Stripped from either end of a name. Honorifics never appear mid-name in
# Indian records, and only stripping at the ends keeps given names that happen
# to collide with a title (Sri Devi) intact when they sit in the middle.
HONORIFICS = frozenset(
    {
        "MR",
        "MRS",
        "MS",
        "MISS",
        "DR",
        "SHRI",
        "SRI",
        "SMT",
        "KUM",
        "LATE",
        "PROF",
    }
)

# The full factor vocabulary. Codes are stable API: they surface in the
# platform contract's `explain.deciding_factors`, so compliance reviewers and
# the Gemini adjudicator prompt both key off these exact strings. Add, never
# rename.
FACTORS = frozenset(
    {
        "exact_match",  # raw inputs identical
        "case_or_punctuation_only",  # differ only in case/punctuation/spacing
        "token_order_differs",  # same tokens, different word order
        "initials_expanded",  # an initial matched a full token (R -> RAJESH)
        "initial_mismatch",  # an initial matched nothing on the other side
        "honorific_stripped",  # a title was removed from at least one side
        "diacritics_folded",  # non-ASCII letters folded on at least one side
        "middle_name_missing",  # one side omits an interior token only
        "extra_token_present",  # one side carries unmatched non-interior tokens
        "surname_differs",  # trailing tokens conflict
        "given_name_differs",  # leading tokens conflict
        "token_overlap_partial",  # some but not all tokens matched
        "no_common_tokens",  # nothing matched, or input was unusable
    }
)

# Signal weights and penalties; see the module docstring for the rationale.
CORE_WEIGHT = 0.70
FUZZY_WEIGHT = 0.30
SHORTER_SIDE_SHARE = 0.75
CONFLICT_WEIGHT = 0.35
OMISSION_PENALTY = 0.06
OMISSION_PENALTY_CAP = 0.18
ORDER_PENALTY = 0.02

# Credit for an initial matching a token it plausibly abbreviates. Below 1.0
# because "R" is compatible with Rajesh, Ramesh and Ravi alike.
INITIAL_CREDIT = 0.85

# Floor for accepting two different spellings as the same token. Calibrated
# against measured pairs: it admits typos and inflections (SHRMA/SHARMA 0.906,
# KRISHNA/KRISHNAN 0.954) while excluding distinct names that string metrics
# rate highly (SUNIL/SUNITA 0.810, PRIYA/PRIYANKA 0.847).
PAIR_FLOOR = 0.88

_VOWELS = frozenset("AEIOU")
_NON_LETTER = re.compile(r"[^A-Z]+")


@dataclass(frozen=True)
class MatchResult:
    """One name comparison: the score, what was compared, and why."""

    score: float
    normalized_input: str
    normalized_registered: str
    factors: list[str]


def normalize_name(raw: str) -> str:
    """Fold a raw name to upper-case ASCII letters and single spaces.

    Diacritics are folded rather than dropped (Rájesh -> RAJESH) and leading or
    trailing honorifics are removed, but never the last remaining token — a
    record reading only "LATE" is bad data, not an empty name.
    """
    return " ".join(_strip_honorifics(_fold(raw)))


def tokenize_name(raw: str) -> list[str]:
    """Normalize, split, and split run-together leading initials apart.

    ``RK SHARMA`` and ``R.K. SHARMA`` must reach the scorer identically, so a
    short vowel-free leading cluster becomes one token per letter. The vowel
    test is what keeps ``JOY SHARMA`` whole; it does misread the abbreviation
    ``MD`` (Mohammed) as two initials, which costs a little credit but never
    invents a match.
    """
    tokens = normalize_name(raw).split()
    if len(tokens) < 2:
        return tokens
    head = tokens[0]
    if (
        2 <= len(head) <= 3
        and not (_VOWELS & set(head))
        and len(tokens[1]) >= 3
    ):
        return list(head) + tokens[1:]
    return tokens


def score_names(input_name: str, registered_name: str) -> MatchResult:
    """Score two names in ``0.0``-``1.0``; never raises, never returns ``None``.

    Unusable input on either side yields ``0.0`` with ``no_common_tokens``
    rather than an exception, because A01 must still emit a contract-shaped
    verdict for a garbage PAN response.
    """
    normalized_input = normalize_name(input_name)
    normalized_registered = normalize_name(registered_name)
    provenance = _provenance_factors(
        input_name, registered_name, normalized_input, normalized_registered
    )

    input_tokens = tokenize_name(input_name)
    registered_tokens = tokenize_name(registered_name)
    if not input_tokens or not registered_tokens:
        return MatchResult(
            score=0.0,
            normalized_input=normalized_input,
            normalized_registered=normalized_registered,
            factors=["no_common_tokens"],
        )

    if normalized_input == normalized_registered:
        identical = isinstance(input_name, str) and input_name == registered_name
        head = ["exact_match"] if identical else ["case_or_punctuation_only"]
        return MatchResult(
            score=1.0,
            normalized_input=normalized_input,
            normalized_registered=normalized_registered,
            factors=head + provenance,
        )

    pairs = _match_tokens(input_tokens, registered_tokens)
    matched_weight = sum(weight for _, _, weight, _ in pairs)
    matched = len(pairs)

    coverage_short = matched_weight / min(len(input_tokens), len(registered_tokens))
    coverage_long = matched_weight / max(len(input_tokens), len(registered_tokens))
    core = (
        SHORTER_SIDE_SHARE * coverage_short
        + (1.0 - SHORTER_SIDE_SHARE) * coverage_long
    )

    aligned_input, aligned_registered = _align_initials(
        input_tokens, registered_tokens, pairs
    )
    fuzzy = fuzz.token_sort_ratio(aligned_input, aligned_registered) / 100.0

    unmatched_input = sorted(set(range(len(input_tokens))) - {i for i, _, _, _ in pairs})
    unmatched_registered = sorted(
        set(range(len(registered_tokens))) - {j for _, j, _, _ in pairs}
    )
    conflicts = min(len(unmatched_input), len(unmatched_registered))
    omissions = abs(len(unmatched_input) - len(unmatched_registered))

    penalty = 0.0
    if conflicts:
        residual = _residual_similarity(
            [input_tokens[i] for i in unmatched_input],
            [registered_tokens[j] for j in unmatched_registered],
            conflicts,
        )
        penalty += CONFLICT_WEIGHT * (1.0 - residual)
    penalty += min(OMISSION_PENALTY * omissions, OMISSION_PENALTY_CAP)

    order_differs = _order_differs(pairs)
    if order_differs:
        penalty += ORDER_PENALTY

    score = CORE_WEIGHT * core + FUZZY_WEIGHT * fuzzy - penalty
    score = min(1.0, max(0.0, score))

    factors = _explain(
        input_tokens=input_tokens,
        registered_tokens=registered_tokens,
        pairs=pairs,
        unmatched_input=unmatched_input,
        unmatched_registered=unmatched_registered,
        conflicts=conflicts,
        omissions=omissions,
        order_differs=order_differs,
    )
    return MatchResult(
        score=round(score, 4),
        normalized_input=normalized_input,
        normalized_registered=normalized_registered,
        factors=factors + provenance,
    )


def _fold(raw: str) -> list[str]:
    """Diacritic-folded, upper-case, letters-only tokens — honorifics intact."""
    if not isinstance(raw, str) or not raw:
        return []
    decomposed = "".join(
        ch
        for ch in unicodedata.normalize("NFKD", raw)
        if not unicodedata.combining(ch)
    )
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii").upper()
    return [token for token in _NON_LETTER.split(ascii_only) if token]


def _strip_honorifics(tokens: list[str]) -> list[str]:
    """Drop titles from both ends, keeping at least one token."""
    kept = list(tokens)
    while len(kept) > 1 and kept[0] in HONORIFICS:
        kept.pop(0)
    while len(kept) > 1 and kept[-1] in HONORIFICS:
        kept.pop()
    return kept


def _provenance_factors(
    raw_input: str,
    raw_registered: str,
    normalized_input: str,
    normalized_registered: str,
) -> list[str]:
    """Factors describing what normalization changed, on either side."""
    del normalized_input, normalized_registered  # derived below from the raws
    raws = [raw for raw in (raw_input, raw_registered) if isinstance(raw, str)]
    factors: list[str] = []
    if any(any(ord(ch) > 127 for ch in raw) for raw in raws):
        factors.append("diacritics_folded")
    if any(len(_strip_honorifics(_fold(raw))) < len(_fold(raw)) for raw in raws):
        factors.append("honorific_stripped")
    return factors


def _pair_similarity(left: str, right: str) -> tuple[float, bool]:
    """Similarity of two tokens, and whether it came from an initial.

    JaroWinkler and the plain indel ratio are averaged: JaroWinkler alone is
    prefix-happy enough to fuse ``SUNIL`` with ``SUNITA``, and the indel ratio
    alone is too harsh on the inflected endings (``KRISHNA``/``KRISHNAN``) that
    Indian records use interchangeably.
    """
    if left == right:
        return 1.0, False
    if len(left) == 1 or len(right) == 1:
        initial, full = (left, right) if len(left) == 1 else (right, left)
        if full.startswith(initial):
            return INITIAL_CREDIT, True
        return 0.0, False
    blend = (
        JaroWinkler.normalized_similarity(left, right) + fuzz.ratio(left, right) / 100.0
    ) / 2.0
    return blend, False


def _candidates(
    left: list[str], right: list[str]
) -> list[tuple[float, bool, int, int]]:
    """All token pairs, ordered by a key that is invariant under argument swap.

    Symmetry is a hard requirement, so the ordering may only use quantities
    that survive exchanging the two names: the similarity, the pair of token
    strings sorted, and ``|i-j|``/``i+j`` rather than ``i`` and ``j``.
    """
    scored: list[tuple[tuple[float, str, str, int, int], float, bool, int, int]] = []
    for i, left_token in enumerate(left):
        for j, right_token in enumerate(right):
            similarity, is_initial = _pair_similarity(left_token, right_token)
            low, high = sorted((left_token, right_token))
            key = (-similarity, low, high, abs(i - j), i + j)
            scored.append((key, similarity, is_initial, i, j))
    scored.sort(key=lambda entry: entry[0])
    return [(sim, is_initial, i, j) for _, sim, is_initial, i, j in scored]


def _match_tokens(
    left: list[str], right: list[str]
) -> list[tuple[int, int, float, bool]]:
    """Greedily pair tokens above the acceptance floor, best pairs first."""
    used_left: set[int] = set()
    used_right: set[int] = set()
    pairs: list[tuple[int, int, float, bool]] = []
    for similarity, is_initial, i, j in _candidates(left, right):
        if similarity < PAIR_FLOOR and not is_initial:
            continue  # an initial pair can sort after a rejected fuzzy pair
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        pairs.append((i, j, similarity, is_initial))
    pairs.sort()
    return pairs


def _residual_similarity(
    left: list[str], right: list[str], conflicts: int
) -> float:
    """Mean similarity of the contradicting tokens, best pairing first.

    Used to scale the conflict penalty: two spellings of one name should cost
    far less than two different names.
    """
    used_left: set[int] = set()
    used_right: set[int] = set()
    scores: list[float] = []
    for similarity, _, i, j in _candidates(left, right):
        if i in used_left or j in used_right:
            continue
        used_left.add(i)
        used_right.add(j)
        scores.append(similarity)
        if len(scores) == conflicts:
            break
    return sum(scores) / len(scores) if scores else 0.0


def _align_initials(
    left: list[str], right: list[str], pairs: list[tuple[int, int, float, bool]]
) -> tuple[str, str]:
    """Expand matched initials on both sides before the string comparison.

    Without this, ``R K SHARMA`` vs ``RAJESH KUMAR SHARMA`` scores 0.69 on
    ``token_sort_ratio`` purely for being abbreviated — a difference
    ``token_core`` has already priced correctly.
    """
    aligned_left = list(left)
    aligned_right = list(right)
    for i, j, _, is_initial in pairs:
        if not is_initial:
            continue
        if len(left[i]) == 1:
            aligned_left[i] = right[j]
        else:
            aligned_right[j] = left[i]
    return " ".join(aligned_left), " ".join(aligned_right)


def _order_differs(pairs: list[tuple[int, int, float, bool]]) -> bool:
    """True when matched tokens appear in a different sequence on each side."""
    right_indices = [j for _, j, _, _ in pairs]
    return any(
        right_indices[k] > right_indices[k + 1] for k in range(len(right_indices) - 1)
    )


def _explain(
    *,
    input_tokens: list[str],
    registered_tokens: list[str],
    pairs: list[tuple[int, int, float, bool]],
    unmatched_input: list[int],
    unmatched_registered: list[int],
    conflicts: int,
    omissions: int,
    order_differs: bool,
) -> list[str]:
    """Build the factor list in a fixed, symmetric order."""
    factors: list[str] = []
    if not pairs:
        return ["no_common_tokens"]
    if order_differs:
        factors.append("token_order_differs")
    if any(is_initial for _, _, _, is_initial in pairs):
        factors.append("initials_expanded")
    if any(len(input_tokens[i]) == 1 for i in unmatched_input) or any(
        len(registered_tokens[j]) == 1 for j in unmatched_registered
    ):
        factors.append("initial_mismatch")
    if conflicts:
        # Conflicts at the same end of both names name the field that differs.
        if (
            len(input_tokens) - 1 in unmatched_input
            and len(registered_tokens) - 1 in unmatched_registered
        ):
            factors.append("surname_differs")
        if 0 in unmatched_input and 0 in unmatched_registered:
            factors.append("given_name_differs")
    if omissions:
        longer, indices = (
            (input_tokens, unmatched_input)
            if len(unmatched_input) > len(unmatched_registered)
            else (registered_tokens, unmatched_registered)
        )
        interior = [idx for idx in indices if 0 < idx < len(longer) - 1]
        if conflicts == 0 and len(interior) == len(indices):
            factors.append("middle_name_missing")
        else:
            factors.append("extra_token_present")
    if len(pairs) < max(len(input_tokens), len(registered_tokens)):
        factors.append("token_overlap_partial")
    return factors
