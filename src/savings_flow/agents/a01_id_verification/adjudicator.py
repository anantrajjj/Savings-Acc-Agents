"""Gemini adjudication of the name match for A01 — Identity Verification.

Deterministic code owns PAN validation, the registry lookup and the response
envelope. Only one question is delegated to a model: *do these two names denote
the same person?* Indian naming reality — initials standing in for expanded
given names, surname-first ordering, transliteration variants (Lakshmi/Laxmi),
honorifics carried into the registry, married-name changes — is precisely where
character-level fuzzy matching mis-scores, and it is the one place a language
model has an edge over `rapidfuzz`.

The model returns a narrow judgement (`score`, `factors`, `reasoning`) and
nothing else; it never authors the platform response. The platform validates
A01's output and fails the run on mismatch, so a model free-typing JSON into the
envelope is unacceptable — hence the response schema, the post-parse validation
and the clamp below.

A01 favours a fast deterministic answer over a slow perfect one: one attempt, no
retry loop, a request timeout, and any failure whatsoever falls back to the
deterministic baseline rather than stalling account opening.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    # Type-only: keeps `import adjudicator` working while `matching` is authored
    # in parallel, and keeps this module importable with no GCP credentials.
    from savings_flow.agents.a01_id_verification.matching import MatchResult

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.7-flash"
# gemini-3.7-flash is served on the global endpoint only; a regional location
# would 404 rather than degrade, so "global" is the default, not an accident.
DEFAULT_LOCATION = "global"
DEFAULT_PROJECT = "sandboxa1"

# One shot, then fall back. A name match is worth ~8s of the customer's time.
REQUEST_TIMEOUT_MS = 8_000

# How far above the deterministic baseline the model is allowed to lift a score.
# A model arguing "R. K. Sharma" is "Rajesh Kumar Sharma" is doing its job; a
# model lifting 0.30 to 0.95 has either hallucinated a relationship between two
# unrelated names or been talked into it by the input, and in KYC a false accept
# is far more expensive than a false reject. The reasoning is still kept so a
# reviewer can see the argument that was made and overrule it by hand.
MAX_MODEL_UPGRADE = 0.25
CLAMP_FACTOR = "model_score_clamped_to_baseline_margin"

# Distinct per failure class so ops can tell a credentials problem from a
# hallucinating model in the logs without reading the surrounding trace.
FALLBACK_CLIENT_UNAVAILABLE = "client_construction_failed"
FALLBACK_API_ERROR = "api_call_failed"
FALLBACK_EMPTY_RESPONSE = "empty_model_response"
FALLBACK_UNPARSEABLE = "unparseable_model_json"
FALLBACK_SCHEMA_VIOLATION = "schema_violation"
FALLBACK_SCORE_OUT_OF_RANGE = "score_out_of_range"
FALLBACK_EMPTY_REASONING = "empty_reasoning"

_SYSTEM_RULES = """\
You adjudicate whether two Indian personal names denote the same individual, for
a bank savings-account KYC check. One name is typed on the application form; the
other is held on the PAN (tax) record.

Treat these as expected variation between the same person's names:
- initials standing in for expanded given names (R. K. Sharma / Rajesh Kumar Sharma)
- surname-first ordering, common in South Indian records (Iyer Padmanabhan / Padmanabhan Iyer)
- transliteration variants of the same name (Lakshmi/Laxmi, Mohammed/Muhammad, Ilyas/Ilias)
- honorifics and titles on either side (Dr., Shri, Smt., Kum.)
- spacing, punctuation and casing differences
- a married surname on the form against a maiden surname on the record, where the
  given names still agree

Treat these as evidence of different people:
- different given names that are not transliterations of one another
- a shared surname with unrelated given names (Anjali Deshpande / Mahesh Deshpande)
- an individual's name against a company, firm or HUF entity name

Be conservative. A false accept lets an impostor open an account; a false reject
only sends the case to a human reviewer. When the evidence is thin, score low.
"""


class _ModelJudgement(BaseModel):
    """The only thing the model is allowed to say.

    Bounds are checked in code, not declared here: an out-of-range score must be
    distinguishable from a malformed payload in the fallback reason.
    """

    model_config = ConfigDict(extra="forbid")

    score: float = Field(description="0.0-1.0 likelihood the names are the same person")
    factors: list[str] = Field(
        description="short snake_case codes for the observed variations, e.g. initials_expanded"
    )
    reasoning: str = Field(description="one to three sentences justifying the score")


@dataclass(frozen=True)
class Adjudication:
    """The adjudicated name match, whoever decided it."""

    score: float
    factors: list[str]
    reasoning: str
    source: str
    model: str | None
    fallback_reason: str | None


class NameMatchAdjudicator:
    """Adjudicates a name match with Gemini, falling back to the baseline.

    The client is built on first use, so constructing an adjudicator — at import
    time, in a test, in a Cloud Run container that has not yet been granted a
    service account — never touches credentials or the network.
    """

    def __init__(
        self,
        client: object | None = None,
        *,
        model: str | None = None,
        project: str | None = None,
        location: str | None = None,
    ) -> None:
        self._client = client
        self._client_failed = False
        self._model = model or os.environ.get("A01_MODEL") or DEFAULT_MODEL
        self._project = project or os.environ.get("GOOGLE_CLOUD_PROJECT") or DEFAULT_PROJECT
        self._location = location or os.environ.get("GOOGLE_CLOUD_LOCATION") or DEFAULT_LOCATION

    def adjudicate(
        self, *, full_name: str, registered_name: str, baseline: MatchResult
    ) -> Adjudication:
        """Return the score to use downstream. Never raises.

        Every failure path yields the deterministic baseline with a specific
        `fallback_reason`, because A01 must always be able to answer.
        """
        client = self._resolve_client()
        if client is None:
            return self._fallback(baseline, FALLBACK_CLIENT_UNAVAILABLE)

        prompt = build_prompt(
            full_name=full_name, registered_name=registered_name, baseline=baseline
        )
        try:
            response = client.models.generate_content(  # type: ignore[attr-defined]
                model=self._model,
                contents=prompt,
                config=self._generation_config(),
            )
        except Exception:  # noqa: BLE001 - vendor SDK raises a wide surface
            logger.warning("A01 name-match model call failed; using baseline", exc_info=True)
            return self._fallback(baseline, FALLBACK_API_ERROR)

        judgement, reason = _parse_judgement(response)
        if judgement is None:
            logger.warning("A01 name-match model response rejected (%s); using baseline", reason)
            return self._fallback(baseline, reason or FALLBACK_SCHEMA_VIOLATION)

        return self._from_judgement(judgement, baseline)

    def _resolve_client(self) -> object | None:
        """Build the Vertex client once, on first use; None if it cannot be built."""
        if self._client is not None or self._client_failed:
            return self._client
        try:
            # Imported here so a missing/broken SDK degrades to fallback at call
            # time rather than breaking `import adjudicator`.
            from google import genai

            self._client = genai.Client(
                vertexai=True, project=self._project, location=self._location
            )
        except Exception:  # noqa: BLE001 - credentials, quota project, bad location
            logger.warning("A01 could not construct a Gemini client; using baseline", exc_info=True)
            # Latch, so a per-request failure is not retried on every request.
            self._client_failed = True
            return None
        return self._client

    def _generation_config(self) -> types.GenerateContentConfig:
        """Constrain the model to the judgement schema and nothing else."""
        return types.GenerateContentConfig(
            system_instruction=_SYSTEM_RULES,
            response_mime_type="application/json",
            response_schema=_ModelJudgement,
            # Name adjudication should be reproducible for the audit trail.
            temperature=0.0,
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )

    def _from_judgement(self, judgement: _ModelJudgement, baseline: MatchResult) -> Adjudication:
        """Accept the model's judgement, clamping an implausible upgrade."""
        factors = list(judgement.factors)
        score = judgement.score
        ceiling = baseline.score + MAX_MODEL_UPGRADE
        if score > ceiling:
            logger.warning(
                "A01 clamped model name-match score %.3f to %.3f (baseline %.3f)",
                score,
                ceiling,
                baseline.score,
            )
            score = min(ceiling, 1.0)
            factors.append(CLAMP_FACTOR)
        return Adjudication(
            score=score,
            factors=factors,
            reasoning=judgement.reasoning,
            source="gemini",
            model=self._model,
            fallback_reason=None,
        )

    def _fallback(self, baseline: MatchResult, reason: str) -> Adjudication:
        """The deterministic answer, labelled so the explain block stays honest."""
        return Adjudication(
            score=baseline.score,
            factors=list(baseline.factors),
            reasoning=(
                f"Deterministic name matching scored {baseline.score:.2f} comparing "
                f"{baseline.normalized_input!r} against {baseline.normalized_registered!r}; "
                f"model adjudication was not used ({reason})."
            ),
            source="deterministic_fallback",
            model=None,
            fallback_reason=reason,
        )


def build_prompt(*, full_name: str, registered_name: str, baseline: MatchResult) -> str:
    """The adjudication request. Mentions no field outside the judgement schema."""
    factors = ", ".join(baseline.factors) if baseline.factors else "none"
    return (
        "Application form name: "
        f"{full_name!r}\n"
        "PAN record name: "
        f"{registered_name!r}\n\n"
        "A deterministic matcher already compared normalised forms of these names:\n"
        f"- normalised form name: {baseline.normalized_input!r}\n"
        f"- normalised record name: {baseline.normalized_registered!r}\n"
        f"- baseline score: {baseline.score:.3f}\n"
        f"- baseline factors: {factors}\n\n"
        "That matcher works on characters, so it under-scores legitimate Indian "
        "name variation and over-scores unrelated names that happen to share a "
        "surname. Use it as a prior, correct it where the naming conventions "
        "account for the difference, and justify any correction.\n\n"
        "Reply with the score (0.0-1.0), the factor codes you observed, and one "
        "to three sentences of reasoning."
    )


def _parse_judgement(response: Any) -> tuple[_ModelJudgement | None, str | None]:
    """Validate the model's reply, returning either a judgement or a reason."""
    if response is None:
        return None, FALLBACK_EMPTY_RESPONSE
    text = getattr(response, "text", None)
    if not text or not str(text).strip():
        return None, FALLBACK_EMPTY_RESPONSE

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None, FALLBACK_UNPARSEABLE
    if not isinstance(payload, dict):
        return None, FALLBACK_SCHEMA_VIOLATION

    try:
        judgement = _ModelJudgement.model_validate(payload)
    except ValidationError:
        return None, FALLBACK_SCHEMA_VIOLATION

    # Checked after parsing so ops can distinguish "model went out of bounds"
    # from "model returned the wrong shape entirely".
    if not 0.0 <= judgement.score <= 1.0:
        return None, FALLBACK_SCORE_OUT_OF_RANGE
    if not judgement.reasoning.strip():
        return None, FALLBACK_EMPTY_REASONING
    return judgement, None
