"""Platform contract for A01 — Identity Verification Agent.

The Gemini Enterprise platform validates every response against this shape and
fails the run on mismatch, so these models *are* the contract. Field names use
the platform's camelCase deliberately — do not rename them to be Pythonic.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A match at or above this scores as verified.
VERIFIED_THRESHOLD = 0.85
# Below verified but at or above this is referral-grade rather than a rejection.
# The contract has no third state, so the distinction is carried in `explain`.
REFERRAL_FLOOR = 0.65


class A01Input(BaseModel):
    """Agent input. Unknown keys are ignored — the workflow around A01 grows."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    pan: str
    fullName: str

    @field_validator("pan", "fullName")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be blank")
        return value


class Explain(BaseModel):
    """Compliance-facing rationale. Every field is required by the platform."""

    model_config = ConfigDict(extra="forbid")

    reasoning_summary: str
    evidence_refs: list[str] = Field(default_factory=list)
    policy_citations: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    deciding_factors: list[str] = Field(default_factory=list)


class A01Output(BaseModel):
    """Agent output. `registeredName` is the only optional key."""

    model_config = ConfigDict(extra="forbid")

    verified: bool
    nameMatchScore: float = Field(ge=0.0, le=1.0)
    registeredName: str | None = None
    explain: Explain

    def to_payload(self) -> dict[str, Any]:
        """Serialise for the wire, omitting `registeredName` when unknown."""
        payload = self.model_dump()
        if payload.get("registeredName") is None:
            payload.pop("registeredName", None)
        payload["nameMatchScore"] = round(payload["nameMatchScore"], 3)
        return payload
