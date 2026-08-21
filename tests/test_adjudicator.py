"""Tests for A01's Gemini name-match adjudicator.

No network and no credentials: every test either injects a fake client exposing
`.models.generate_content(...)` or forces client construction to fail. The
contract under test is that `adjudicate` always returns an `Adjudication` and
labels where the score came from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from savings_flow.agents.a01_id_verification.adjudicator import (
    CLAMP_FACTOR,
    DEFAULT_LOCATION,
    DEFAULT_MODEL,
    DEFAULT_PROJECT,
    FALLBACK_API_ERROR,
    FALLBACK_CLIENT_UNAVAILABLE,
    FALLBACK_EMPTY_REASONING,
    FALLBACK_EMPTY_RESPONSE,
    FALLBACK_SCHEMA_VIOLATION,
    FALLBACK_SCORE_OUT_OF_RANGE,
    FALLBACK_UNPARSEABLE,
    MAX_MODEL_UPGRADE,
    Adjudication,
    NameMatchAdjudicator,
    build_prompt,
)


# Local stand-in for `matching.MatchResult` — the adjudicator only reads these
# four attributes, so the tests stay independent of that module's internals.
@dataclass(frozen=True)
class StubMatchResult:
    score: float
    normalized_input: str = "rajesh kumar sharma"
    normalized_registered: str = "r k sharma"
    factors: list[str] = field(default_factory=lambda: ["initials_expanded"])


def baseline(score: float = 0.62, **kwargs: Any) -> StubMatchResult:
    return StubMatchResult(score=score, **kwargs)


class FakeModels:
    """Records the request and replays a canned reply or exception."""

    def __init__(self, *, text: str | None = None, exc: Exception | None = None) -> None:
        self._text = text
        self._exc = exc
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(text=self._text)


class FakeClient:
    def __init__(self, *, text: str | None = None, exc: Exception | None = None) -> None:
        self.models = FakeModels(text=text, exc=exc)


def judgement_json(
    score: float = 0.94,
    factors: list[str] | None = None,
    reasoning: str = "Initials on the form expand to the registered given names.",
) -> str:
    return json.dumps(
        {
            "score": score,
            "factors": ["initials_expanded"] if factors is None else factors,
            "reasoning": reasoning,
        }
    )


def adjudicate(
    client: object, *, base: StubMatchResult | None = None, **kwargs: Any
) -> Adjudication:
    adj = NameMatchAdjudicator(client, **kwargs)
    return adj.adjudicate(
        full_name="R. K. Sharma",
        registered_name="Rajesh Kumar Sharma",
        baseline=base if base is not None else baseline(),
    )


def test_module_defaults_are_the_documented_ones() -> None:
    assert DEFAULT_MODEL == "gemini-3.7-flash"
    assert DEFAULT_LOCATION == "global"  # gemini-3.7-flash is global-endpoint only
    assert DEFAULT_PROJECT == "sandboxa1"


def test_happy_path_uses_the_model_result() -> None:
    client = FakeClient(text=judgement_json(score=0.81, factors=["initials_expanded"]))

    result = adjudicate(client, base=baseline(0.62))

    assert result.score == pytest.approx(0.81)
    assert result.factors == ["initials_expanded"]
    assert result.reasoning.startswith("Initials on the form")
    assert result.source == "gemini"
    assert result.model == DEFAULT_MODEL
    assert result.fallback_reason is None


def test_request_carries_response_schema_mime_type_and_model_id() -> None:
    client = FakeClient(text=judgement_json(score=0.7))

    adjudicate(client, model="gemini-3.7-flash-custom")

    (call,) = client.models.calls
    assert call["model"] == "gemini-3.7-flash-custom"
    config = call["config"]
    assert config.response_schema is not None
    assert config.response_mime_type == "application/json"
    # One attempt with a bounded timeout: A01 prefers a fast deterministic answer.
    assert config.http_options.timeout > 0
    assert config.temperature == 0.0


def test_prompt_carries_both_names_and_the_baseline_but_no_envelope_fields() -> None:
    prompt = build_prompt(
        full_name="R. K. Sharma",
        registered_name="Rajesh Kumar Sharma",
        baseline=baseline(0.62, factors=["initials_expanded", "token_subset"]),
    )

    assert "R. K. Sharma" in prompt
    assert "Rajesh Kumar Sharma" in prompt
    assert "0.620" in prompt
    assert "initials_expanded" in prompt and "token_subset" in prompt
    # The model must never be invited to author the platform response.
    for envelope_field in ("verified", "nameMatchScore", "registeredName", "explain"):
        assert envelope_field not in prompt


def test_unparseable_json_falls_back() -> None:
    result = adjudicate(FakeClient(text="not json at all {{"), base=baseline(0.62))

    assert result.source == "deterministic_fallback"
    assert result.fallback_reason == FALLBACK_UNPARSEABLE
    assert result.model is None
    assert result.score == pytest.approx(0.62)
    assert result.factors == ["initials_expanded"]
    assert "0.62" in result.reasoning


def test_json_that_is_not_an_object_falls_back_as_schema_violation() -> None:
    result = adjudicate(FakeClient(text="[0.9]"))

    assert result.fallback_reason == FALLBACK_SCHEMA_VIOLATION
    assert result.source == "deterministic_fallback"


def test_api_exception_falls_back() -> None:
    result = adjudicate(FakeClient(exc=RuntimeError("503 backend unavailable")))

    assert result.source == "deterministic_fallback"
    assert result.fallback_reason == FALLBACK_API_ERROR
    assert result.model is None


def test_timeout_exception_falls_back_without_retrying() -> None:
    client = FakeClient(exc=TimeoutError("deadline exceeded"))

    result = adjudicate(client)

    assert result.fallback_reason == FALLBACK_API_ERROR
    assert len(client.models.calls) == 1


@pytest.mark.parametrize("score", [1.4, -0.2, 12.0])
def test_score_out_of_range_falls_back(score: float) -> None:
    result = adjudicate(FakeClient(text=judgement_json(score=score)), base=baseline(0.5))

    assert result.fallback_reason == FALLBACK_SCORE_OUT_OF_RANGE
    assert result.source == "deterministic_fallback"
    assert result.score == pytest.approx(0.5)


@pytest.mark.parametrize("text", [None, "", "   \n"])
def test_empty_model_text_falls_back(text: str | None) -> None:
    result = adjudicate(FakeClient(text=text))

    assert result.fallback_reason == FALLBACK_EMPTY_RESPONSE
    assert result.source == "deterministic_fallback"


def test_response_object_without_text_falls_back() -> None:
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: object()))

    result = adjudicate(client)

    assert result.fallback_reason == FALLBACK_EMPTY_RESPONSE


def test_none_response_falls_back() -> None:
    client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **_: None))

    result = adjudicate(client)

    assert result.fallback_reason == FALLBACK_EMPTY_RESPONSE


@pytest.mark.parametrize(
    "payload",
    [
        {"factors": ["a"], "reasoning": "no score at all"},
        {"score": 0.9, "reasoning": "factors missing"},
        {"score": 0.9, "factors": "initials_expanded", "reasoning": "factors not a list"},
        {"score": 0.9, "factors": [1, 2], "reasoning": "factors not strings"},
        {"score": "high", "factors": ["a"], "reasoning": "score not a number"},
        {"score": 0.9, "factors": ["a"], "reasoning": "extra key", "verified": True},
    ],
)
def test_schema_violations_fall_back(payload: dict[str, Any]) -> None:
    result = adjudicate(FakeClient(text=json.dumps(payload)), base=baseline(0.44))

    assert result.fallback_reason == FALLBACK_SCHEMA_VIOLATION
    assert result.source == "deterministic_fallback"
    assert result.score == pytest.approx(0.44)


def test_blank_reasoning_falls_back() -> None:
    result = adjudicate(FakeClient(text=judgement_json(reasoning="   ")))

    assert result.fallback_reason == FALLBACK_EMPTY_REASONING
    assert result.source == "deterministic_fallback"


def test_client_construction_failure_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(**_: Any) -> Any:
        raise RuntimeError("could not find default credentials")

    monkeypatch.setattr("google.genai.Client", explode)

    result = NameMatchAdjudicator().adjudicate(
        full_name="R. K. Sharma", registered_name="Rajesh Kumar Sharma", baseline=baseline(0.62)
    )

    assert result.source == "deterministic_fallback"
    assert result.fallback_reason == FALLBACK_CLIENT_UNAVAILABLE
    assert result.model is None
    assert result.score == pytest.approx(0.62)


def test_client_construction_is_attempted_once_then_latched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock(side_effect=RuntimeError("no credentials"))
    monkeypatch.setattr("google.genai.Client", factory)
    adj = NameMatchAdjudicator()

    for _ in range(3):
        assert adj.adjudicate(
            full_name="A", registered_name="B", baseline=baseline(0.1)
        ).fallback_reason == FALLBACK_CLIENT_UNAVAILABLE

    assert factory.call_count == 1


def test_injected_client_means_no_client_is_ever_constructed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock(side_effect=AssertionError("must not build a client"))
    monkeypatch.setattr("google.genai.Client", factory)

    result = adjudicate(FakeClient(text=judgement_json(score=0.7)))

    assert result.source == "gemini"
    assert factory.call_count == 0


def test_lazy_client_is_built_with_vertex_project_and_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = MagicMock(return_value=FakeClient(text=judgement_json(score=0.7)))
    monkeypatch.setattr("google.genai.Client", factory)

    result = NameMatchAdjudicator(project="proj-x", location="global").adjudicate(
        full_name="A", registered_name="B", baseline=baseline(0.6)
    )

    assert result.source == "gemini"
    factory.assert_called_once_with(vertexai=True, project="proj-x", location="global")


def test_model_score_far_above_baseline_is_clamped() -> None:
    client = FakeClient(text=judgement_json(score=0.99, factors=["surname_reordered"]))

    result = adjudicate(client, base=baseline(0.30))

    # Kept the model's argument, refused its number: a false accept in KYC costs
    # far more than a referral.
    assert result.score == pytest.approx(0.30 + MAX_MODEL_UPGRADE)
    assert CLAMP_FACTOR in result.factors
    assert "surname_reordered" in result.factors
    assert result.source == "gemini"
    assert result.model == DEFAULT_MODEL
    assert result.fallback_reason is None


def test_upgrade_within_the_margin_is_not_clamped() -> None:
    result = adjudicate(FakeClient(text=judgement_json(score=0.85)), base=baseline(0.62))

    assert result.score == pytest.approx(0.85)
    assert CLAMP_FACTOR not in result.factors


def test_clamped_score_never_exceeds_one() -> None:
    result = adjudicate(FakeClient(text=judgement_json(score=1.0)), base=baseline(0.95))

    assert result.score <= 1.0
    assert CLAMP_FACTOR not in result.factors


def test_model_lowering_the_score_is_always_honoured() -> None:
    result = adjudicate(FakeClient(text=judgement_json(score=0.12)), base=baseline(0.88))

    assert result.score == pytest.approx(0.12)
    assert CLAMP_FACTOR not in result.factors
    assert result.source == "gemini"


def test_model_id_comes_from_env_when_not_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("A01_MODEL", "gemini-3.7-flash-preview")
    client = FakeClient(text=judgement_json(score=0.7))

    result = adjudicate(client)

    assert result.model == "gemini-3.7-flash-preview"
    assert client.models.calls[0]["model"] == "gemini-3.7-flash-preview"


def test_project_and_location_come_from_env_when_not_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    factory = MagicMock(return_value=FakeClient(text=judgement_json(score=0.7)))
    monkeypatch.setattr("google.genai.Client", factory)

    NameMatchAdjudicator().adjudicate(
        full_name="A", registered_name="B", baseline=baseline(0.6)
    )

    factory.assert_called_once_with(vertexai=True, project="proj-env", location="us-central1")


def test_returned_factors_are_a_fresh_list(monkeypatch: pytest.MonkeyPatch) -> None:
    base = baseline(0.5, factors=["initials_expanded"])

    result = adjudicate(FakeClient(text="broken"), base=base)
    result.factors.append("mutated")

    assert base.factors == ["initials_expanded"]


def test_adjudicate_never_raises_for_a_hostile_client() -> None:
    class Hostile:
        @property
        def models(self) -> Any:
            raise RuntimeError("attribute access blew up")

    result = adjudicate(Hostile())

    assert result.source == "deterministic_fallback"
    assert result.fallback_reason == FALLBACK_API_ERROR


def test_clamp_margin_still_clears_the_single_transliteration_case() -> None:
    """Locks in the calibration the clamp margin was chosen for.

    A single transliterated given name ("Laxmi" vs "Lakshmi") scores ~0.53
    offline; the margin must leave enough headroom for the model to carry that
    over the verification threshold, or the clamp defeats the one case the
    model was added to handle. Two transliterated tokens (~0.43) must still
    fall short — the weaker the offline evidence, the less the model may add.
    """
    from savings_flow.agents.a01_id_verification.contract import VERIFIED_THRESHOLD
    from savings_flow.agents.a01_id_verification.matching import score_names

    single = score_names("Laxmi Narayanan", "Lakshmi Narayanan").score
    assert single + MAX_MODEL_UPGRADE >= VERIFIED_THRESHOLD

    double = score_names("Muhammad Ilias Qureshi", "Mohammed Ilyas Qureshi").score
    assert double + MAX_MODEL_UPGRADE < VERIFIED_THRESHOLD
