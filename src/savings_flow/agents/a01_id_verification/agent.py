"""A01 — Identity Verification Agent (agent_id A01, not part of the agent name).

Verifies an applicant's PAN and that the name they gave matches the name held
against that PAN. Deterministic Python owns every decision that can be made
offline — PAN structure, registry lookup, holder type, record status — and a
single Gemini call adjudicates only the name comparison, where Indian naming
reality defeats fuzzy string matching.

`query` never raises and never returns a partial shape: the Gemini Enterprise
platform re-validates the response and fails the run on mismatch, so every
failure path is expressed *inside* the contract as `verified: false` with a
populated `explain` block.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import ValidationError

from savings_flow.common import policy
from savings_flow.common.pan import PanCheck, check_pan

from .adjudicator import Adjudication, NameMatchAdjudicator
from .contract import (
    REFERRAL_FLOOR,
    VERIFIED_THRESHOLD,
    A01Input,
    A01Output,
    Explain,
)
from .matching import MatchResult, score_names
from .registry import MockPanRegistry, PanRecord, PanRegistry

logger = logging.getLogger(__name__)

# A savings account applicant must be a natural person; a PAN issued to a
# company, trust or HUF cannot verify an individual's identity.
INDIVIDUAL_HOLDER_TYPE = "Individual"

# Evidence source labels. Real integrations replace the source prefix and
# nothing else downstream needs to change.
PAN_FORMAT_SOURCE = "pan_format_check"
NAME_MATCH_SOURCE = "name_match"


class A01Agent:
    """Identity Verification Agent.

    Deployment-agnostic on purpose: Cloud Run serves it through
    `service.py`, and the same class satisfies Vertex Agent Engine's
    `set_up()` / `query()` protocol without modification.
    """

    def __init__(
        self,
        registry: PanRegistry | None = None,
        adjudicator: NameMatchAdjudicator | None = None,
    ) -> None:
        self._registry = registry
        self._adjudicator = adjudicator

    def set_up(self) -> None:
        """Build the mocked registry and the model client holder.

        Called once by the serving layer. The adjudicator constructs its
        Gemini client lazily, so this stays cheap and credential-free.
        """
        if self._registry is None:
            self._registry = MockPanRegistry()
        if self._adjudicator is None:
            self._adjudicator = NameMatchAdjudicator()

    # ------------------------------------------------------------------
    # Contract entrypoint
    # ------------------------------------------------------------------
    def query(self, *, input: dict[str, Any]) -> dict[str, Any]:
        """Verify identity. Returns the platform output shape, always."""
        self.set_up()
        try:
            return self._query(input)
        except Exception:  # pragma: no cover — belt and braces
            # A crash here would fail platform validation, which is worse than
            # an honest negative. Log loudly, answer inside the contract.
            logger.exception("A01 query failed unexpectedly")
            return self._failure(
                reasoning="Verification could not be completed due to an internal error.",
                factors=["internal_error"],
                confidence=0.5,
            ).to_payload()

    def _query(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            request = A01Input.model_validate(raw or {})
        except ValidationError as exc:
            missing = ", ".join(str(err["loc"][0]) for err in exc.errors()) or "input"
            return self._failure(
                reasoning=(
                    f"Request rejected: required identity fields are missing or blank ({missing})."
                ),
                factors=["input_invalid"],
                confidence=1.0,
            ).to_payload()

        pan_check = check_pan(request.pan)
        if not pan_check.valid:
            return self._failure(
                reasoning=f"PAN failed structural validation: {pan_check.reason}.",
                factors=["pan_structurally_invalid"],
                evidence=[f"{PAN_FORMAT_SOURCE}:{pan_check.pan or 'empty'}"],
                confidence=1.0,
            ).to_payload()

        record = self._registry.lookup(pan_check.pan)  # type: ignore[union-attr]
        evidence = [
            f"{PAN_FORMAT_SOURCE}:{pan_check.pan}",
            self._registry.evidence_ref(pan_check.pan),  # type: ignore[union-attr]
        ]
        if record is None:
            return self._failure(
                reasoning=(
                    "PAN is well-formed but no matching record exists in the PAN registry, "
                    "so the applicant's identity could not be corroborated."
                ),
                factors=["pan_not_found_in_registry"],
                evidence=evidence,
                confidence=0.9,
            ).to_payload()

        baseline = score_names(request.fullName, record.registered_name)
        adjudication = self._adjudicator.adjudicate(  # type: ignore[union-attr]
            full_name=request.fullName,
            registered_name=record.registered_name,
            baseline=baseline,
        )
        return self._decide(
            request=request,
            pan_check=pan_check,
            record=record,
            baseline=baseline,
            adjudication=adjudication,
            evidence=evidence,
        ).to_payload()

    # ------------------------------------------------------------------
    # Decision composition
    # ------------------------------------------------------------------
    def _decide(
        self,
        *,
        request: A01Input,
        pan_check: PanCheck,
        record: PanRecord,
        baseline: MatchResult,
        adjudication: Adjudication,
        evidence: list[str],
    ) -> A01Output:
        score = _clamp(adjudication.score)
        factors: list[str] = list(adjudication.factors)
        blockers: list[str] = []

        if record.status != "ACTIVE":
            blockers.append(f"pan_status_{record.status.lower()}")
        if record.holder_type != INDIVIDUAL_HOLDER_TYPE:
            # Position 4 of the PAN encodes this, so it is a hard structural fact.
            blockers.append("holder_type_not_individual")

        name_ok = score >= VERIFIED_THRESHOLD
        if not name_ok:
            factors.append(
                "name_match_referral_band"
                if score >= REFERRAL_FLOOR
                else "name_match_below_threshold"
            )
        factors.extend(blockers)
        if adjudication.source == "deterministic_fallback":
            factors.append("model_unavailable_deterministic_score_used")

        verified = name_ok and not blockers
        evidence = [
            *evidence,
            f"{NAME_MATCH_SOURCE}:{adjudication.source}",
        ]

        return A01Output(
            verified=verified,
            nameMatchScore=score,
            registeredName=record.registered_name,
            explain=Explain(
                reasoning_summary=_summarise(
                    request=request,
                    record=record,
                    score=score,
                    verified=verified,
                    blockers=blockers,
                    adjudication=adjudication,
                ),
                evidence_refs=evidence,
                policy_citations=policy.cite(*policy.ids_for_agent("A01")),
                confidence=_confidence(score, adjudication, blockers),
                deciding_factors=_dedupe(factors),
            ),
        )

    def _failure(
        self,
        *,
        reasoning: str,
        factors: list[str],
        confidence: float,
        evidence: list[str] | None = None,
    ) -> A01Output:
        """A negative answer that still satisfies the contract exactly."""
        return A01Output(
            verified=False,
            nameMatchScore=0.0,
            registeredName=None,
            explain=Explain(
                reasoning_summary=reasoning,
                evidence_refs=evidence or [],
                policy_citations=policy.cite(*policy.ids_for_agent("A01")),
                confidence=_clamp(confidence),
                deciding_factors=factors,
            ),
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving dedupe — factor order is the reviewer's reading order."""
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _confidence(
    score: float, adjudication: Adjudication, blockers: list[str]
) -> float:
    """How sure we are of the verdict, not how well the names matched.

    A score sitting on the threshold is the least certain verdict, so
    confidence rises with distance from it. A blocker (dead PAN, non-individual
    holder) is a structural fact, so it is certain. Falling back to the
    deterministic score costs a little confidence, since the model exists
    precisely for the cases the baseline handles worst.
    """
    if blockers:
        return 1.0
    distance = abs(score - VERIFIED_THRESHOLD)
    confidence = min(1.0, 0.6 + 2.0 * distance)
    if adjudication.source == "deterministic_fallback":
        confidence -= 0.1
    return round(_clamp(confidence), 3)


def _summarise(
    *,
    request: A01Input,
    record: PanRecord,
    score: float,
    verified: bool,
    blockers: list[str],
    adjudication: Adjudication,
) -> str:
    verdict = "Verified" if verified else "Not verified"
    parts = [
        f"{verdict}: submitted name {request.fullName!r} scored {score:.2f} against "
        f"registered name {record.registered_name!r} on PAN {record.pan}."
    ]
    if adjudication.reasoning:
        parts.append(adjudication.reasoning.strip())
    if "pan_status_inactive" in blockers or any(
        b.startswith("pan_status_") for b in blockers
    ):
        parts.append(f"PAN record status is {record.status}, not ACTIVE.")
    if "holder_type_not_individual" in blockers:
        parts.append(
            f"PAN is issued to a {record.holder_type}, which cannot establish an "
            "individual applicant's identity."
        )
    if not verified and not blockers:
        band = "referral" if score >= REFERRAL_FLOOR else "rejection"
        parts.append(
            f"Score falls in the {band} band (verification threshold {VERIFIED_THRESHOLD:.2f})."
        )
    return " ".join(parts)
