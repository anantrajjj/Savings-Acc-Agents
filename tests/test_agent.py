"""Integration tests for the A01 agent — the composition, not the parts.

The model is always stubbed here: these tests must run with no credentials and
no network. What they guard is that every path through `query` returns the
platform contract shape, and that the LLM never gets to dictate the verdict.
"""

from __future__ import annotations

from typing import Any

import pytest

from savings_flow.agents.a01_id_verification.adjudicator import Adjudication
from savings_flow.agents.a01_id_verification.agent import A01Agent
from savings_flow.agents.a01_id_verification.contract import (
    REFERRAL_FLOOR,
    VERIFIED_THRESHOLD,
)
from savings_flow.agents.a01_id_verification.matching import MatchResult
from savings_flow.agents.a01_id_verification.registry import (
    MockPanRegistry,
    PanRecord,
)

from conftest import assert_matches_contract  # pytest puts tests/ on sys.path

ACTIVE_PAN = "ZZBPS1002B"


class StubAdjudicator:
    """Returns a fixed score, recording what it was asked."""

    def __init__(
        self,
        score: float = 0.95,
        source: str = "gemini",
        factors: list[str] | None = None,
        reasoning: str = "Names agree once initials are expanded.",
    ) -> None:
        self.score = score
        self.source = source
        self.factors = factors if factors is not None else ["initials_expanded"]
        self.reasoning = reasoning
        self.calls: list[dict[str, Any]] = []

    def adjudicate(
        self, *, full_name: str, registered_name: str, baseline: MatchResult
    ) -> Adjudication:
        self.calls.append(
            {
                "full_name": full_name,
                "registered_name": registered_name,
                "baseline": baseline,
            }
        )
        return Adjudication(
            score=self.score,
            factors=list(self.factors),
            reasoning=self.reasoning,
            source=self.source,
            model="gemini-3.7-flash" if self.source == "gemini" else None,
            fallback_reason=None if self.source == "gemini" else "stubbed_fallback",
        )


def build_agent(
    *,
    score: float = 0.95,
    source: str = "gemini",
    records: dict[str, PanRecord] | None = None,
) -> tuple[A01Agent, StubAdjudicator]:
    adjudicator = StubAdjudicator(score=score, source=source)
    agent = A01Agent(
        registry=MockPanRegistry(records) if records is not None else MockPanRegistry(),
        adjudicator=adjudicator,
    )
    agent.set_up()
    return agent, adjudicator


def registered_name_for(pan: str) -> str:
    record = MockPanRegistry().lookup(pan)
    assert record is not None
    return record.registered_name


# ----------------------------------------------------------------------
# Every path returns the contract shape
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"pan": ACTIVE_PAN, "fullName": "R. K. Sharma"}, id="happy"),
        pytest.param({}, id="empty-input"),
        pytest.param({"pan": ACTIVE_PAN}, id="missing-name"),
        pytest.param({"fullName": "R. K. Sharma"}, id="missing-pan"),
        pytest.param({"pan": "", "fullName": ""}, id="blank-both"),
        pytest.param({"pan": "NOTAPAN", "fullName": "R. K. Sharma"}, id="bad-pan"),
        pytest.param({"pan": "ZZUPW9999Z", "fullName": "R. K. Sharma"}, id="pan-not-found"),
        pytest.param(
            {"pan": ACTIVE_PAN, "fullName": "R. K. Sharma", "extra": "ignored"},
            id="extra-key",
        ),
    ],
)
def test_every_path_satisfies_the_contract(payload: dict[str, Any]) -> None:
    agent, _ = build_agent()
    assert_matches_contract(agent.query(input=payload))


def test_query_never_raises_on_hostile_input() -> None:
    agent, _ = build_agent()
    for payload in ({}, {"pan": None, "fullName": None}, {"pan": 42, "fullName": []}):
        result = agent.query(input=payload)  # type: ignore[arg-type]
        assert result["verified"] is False
        assert_matches_contract(result)


# ----------------------------------------------------------------------
# Verdicts
# ----------------------------------------------------------------------
def test_high_score_on_active_individual_pan_verifies() -> None:
    agent, _ = build_agent(score=0.95)
    result = agent.query(input={"pan": ACTIVE_PAN, "fullName": "R. K. Sharma"})
    assert result["verified"] is True
    assert result["nameMatchScore"] == pytest.approx(0.95)
    assert result["registeredName"] == registered_name_for(ACTIVE_PAN)


def test_score_just_below_threshold_does_not_verify() -> None:
    agent, _ = build_agent(score=VERIFIED_THRESHOLD - 0.01)
    result = agent.query(input={"pan": ACTIVE_PAN, "fullName": "Rakesh Sharma"})
    assert result["verified"] is False
    assert "name_match_referral_band" in result["explain"]["deciding_factors"]


def test_score_below_referral_floor_is_a_rejection_not_a_referral() -> None:
    agent, _ = build_agent(score=REFERRAL_FLOOR - 0.1)
    result = agent.query(input={"pan": ACTIVE_PAN, "fullName": "Someone Else"})
    assert result["verified"] is False
    factors = result["explain"]["deciding_factors"]
    assert "name_match_below_threshold" in factors
    assert "name_match_referral_band" not in factors


def test_invalid_pan_never_reaches_the_model() -> None:
    agent, adjudicator = build_agent()
    result = agent.query(input={"pan": "12345ABCDE", "fullName": "R. K. Sharma"})
    assert result["verified"] is False
    assert adjudicator.calls == []
    assert "pan_structurally_invalid" in result["explain"]["deciding_factors"]


def test_unknown_pan_never_reaches_the_model_and_reports_no_name() -> None:
    agent, adjudicator = build_agent()
    result = agent.query(input={"pan": "ZZUPW9999Z", "fullName": "R. K. Sharma"})
    assert adjudicator.calls == []
    assert "registeredName" not in result
    assert "pan_not_found_in_registry" in result["explain"]["deciding_factors"]


@pytest.mark.parametrize("status", ["INACTIVE", "DEACTIVATED"])
def test_non_active_pan_cannot_verify_however_well_the_name_matches(status: str) -> None:
    pan = "ZZBPS1002B"
    records = {
        pan: PanRecord(
            pan=pan,
            registered_name="Rajesh Kumar Sharma",
            holder_type="Individual",
            status=status,
        )
    }
    agent, _ = build_agent(score=1.0, records=records)
    result = agent.query(input={"pan": pan, "fullName": "Rajesh Kumar Sharma"})
    assert result["verified"] is False
    assert f"pan_status_{status.lower()}" in result["explain"]["deciding_factors"]
    # A structural blocker is a certainty, not a judgement call.
    assert result["explain"]["confidence"] == 1.0


def test_non_individual_pan_cannot_verify_an_individual() -> None:
    pan = "ZZPCA1014P"
    agent, _ = build_agent(score=1.0)
    record = MockPanRegistry().lookup(pan)
    assert record is not None and record.holder_type != "Individual"
    result = agent.query(input={"pan": pan, "fullName": record.registered_name})
    assert result["verified"] is False
    assert "holder_type_not_individual" in result["explain"]["deciding_factors"]


# ----------------------------------------------------------------------
# The model informs, it does not decide
# ----------------------------------------------------------------------
def test_model_receives_both_names_and_the_deterministic_baseline() -> None:
    agent, adjudicator = build_agent()
    agent.query(input={"pan": ACTIVE_PAN, "fullName": "R. K. Sharma"})
    assert len(adjudicator.calls) == 1
    call = adjudicator.calls[0]
    assert call["full_name"] == "R. K. Sharma"
    assert call["registered_name"] == registered_name_for(ACTIVE_PAN)
    assert isinstance(call["baseline"], MatchResult)


def test_deterministic_fallback_is_disclosed_and_costs_confidence() -> None:
    agent_model, _ = build_agent(score=0.95, source="gemini")
    agent_fallback, _ = build_agent(score=0.95, source="deterministic_fallback")
    payload = {"pan": ACTIVE_PAN, "fullName": "R. K. Sharma"}

    with_model = agent_model.query(input=payload)
    with_fallback = agent_fallback.query(input=payload)

    factors = with_fallback["explain"]["deciding_factors"]
    assert "model_unavailable_deterministic_score_used" in factors
    assert (
        with_fallback["explain"]["confidence"] < with_model["explain"]["confidence"]
    )
    # Disclosure changes the explanation, never the verdict.
    assert with_fallback["verified"] == with_model["verified"] is True


def test_explain_is_populated_on_every_path() -> None:
    agent, _ = build_agent()
    for payload in (
        {"pan": ACTIVE_PAN, "fullName": "R. K. Sharma"},
        {"pan": "NOTAPAN", "fullName": "X"},
        {"pan": "ZZUPW9999Z", "fullName": "X"},
        {},
    ):
        explain = agent.query(input=payload)["explain"]
        assert explain["reasoning_summary"].strip()
        assert explain["policy_citations"], "a KYC decision must cite policy"
        assert explain["deciding_factors"], "a reviewer needs the drivers"


def test_evidence_refs_use_the_source_colon_identifier_convention() -> None:
    agent, _ = build_agent()
    refs = agent.query(input={"pan": ACTIVE_PAN, "fullName": "R. K. Sharma"})[
        "explain"
    ]["evidence_refs"]
    assert refs, "a verified identity must cite its evidence"
    for ref in refs:
        source, _, identifier = ref.partition(":")
        assert source and identifier, f"malformed evidence ref: {ref!r}"


def test_set_up_is_idempotent_and_query_calls_it() -> None:
    agent = A01Agent(adjudicator=StubAdjudicator())
    agent.set_up()
    agent.set_up()
    assert_matches_contract(
        agent.query(input={"pan": ACTIVE_PAN, "fullName": "R. K. Sharma"})
    )
