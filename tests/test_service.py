"""Tests for the A01 HTTP surface (the Cloud Run twin of `:query`)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from savings_flow.agents.a01_id_verification.contract import A01Output, Explain
from savings_flow.agents.a01_id_verification.service import create_app

# A real, contract-valid output: the envelope must carry it through untouched.
OUTPUT = A01Output(
    verified=True,
    nameMatchScore=0.93,
    registeredName="Rajesh Kumar Sharma",
    explain=Explain(
        reasoning_summary="PAN is live and the applicant name matches the record.",
        evidence_refs=["mock_pan_registry:ABCPE1234F"],
        policy_citations=["RBI Master Direction KYC 2016 s.16"],
        confidence=0.93,
        deciding_factors=["pan_status=ACTIVE", "name_match=0.93"],
    ),
).to_payload()

PLATFORM_BODY: dict[str, Any] = {
    "class_method": "query",
    "input": {"input": {"pan": "ABCPE1234F", "fullName": "Rajesh Kumar Sharma"}},
}


class StubAgent:
    """Records how it was called; never touches a model."""

    def __init__(self, error: Exception | None = None) -> None:
        self.set_up_calls = 0
        self.calls: list[dict[str, Any]] = []
        self._error = error

    def set_up(self) -> None:
        self.set_up_calls += 1

    def query(self, **kwargs: Any) -> dict[str, Any]:
        # Captured as **kwargs so the tests can assert the keyword *name* the
        # service uses, not just the value it passes.
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return dict(OUTPUT)


@pytest.fixture
def agent() -> StubAgent:
    return StubAgent()


@pytest.fixture
def client(agent: StubAgent) -> TestClient:
    return TestClient(create_app(agent))


def test_platform_envelope_round_trip(client: TestClient) -> None:
    response = client.post("/query", json=PLATFORM_BODY)

    assert response.status_code == 200
    assert response.json() == {"output": OUTPUT}


def test_agent_receives_inner_payload_as_input_kwarg(
    client: TestClient, agent: StubAgent
) -> None:
    client.post("/query", json=PLATFORM_BODY)

    # Exactly the inner map, under exactly the kwarg name `input` — the double
    # nesting is unwrapped once, not zero or twice.
    assert agent.calls == [
        {"input": {"pan": "ABCPE1234F", "fullName": "Rajesh Kumar Sharma"}}
    ]


@pytest.mark.parametrize("class_method", ["stream_query", "Query", "", "register"])
def test_wrong_class_method_is_rejected(
    client: TestClient, agent: StubAgent, class_method: str
) -> None:
    response = client.post(
        "/query", json={"class_method": class_method, "input": {"input": {}}}
    )

    assert response.status_code == 400
    assert "class_method" in response.json()["detail"]
    assert agent.calls == [], "a rejected envelope must never reach the agent"


@pytest.mark.parametrize(
    "outer",
    [{}, {"input": {}}, {"input": None}, {"other": {"pan": "X"}}],
    ids=["no-input-key", "empty-input", "null-input", "wrong-key"],
)
def test_missing_inner_payload_becomes_empty_dict(
    client: TestClient, agent: StubAgent, outer: dict[str, Any]
) -> None:
    # The agent validates its own input, so it must be handed a dict rather
    # than None — a malformed call is the agent's rejection to make, not a 500.
    response = client.post("/query", json={"class_method": "query", "input": outer})

    assert response.status_code == 200
    assert agent.calls == [{"input": {}}]


def test_unknown_top_level_body_keys_are_ignored(
    client: TestClient, agent: StubAgent
) -> None:
    body = dict(PLATFORM_BODY) | {"trace_id": "abc123", "config": {"retries": 2}}

    response = client.post("/query", json=body)

    assert response.status_code == 200
    assert response.json() == {"output": OUTPUT}
    assert agent.calls == [
        {"input": {"pan": "ABCPE1234F", "fullName": "Rajesh Kumar Sharma"}}
    ]


def test_agent_exception_returns_500_without_a_stack_trace() -> None:
    boom = RuntimeError("model client exploded at /secret/path/agent.py:42")
    client = TestClient(create_app(StubAgent(error=boom)), raise_server_exceptions=False)

    response = client.post("/query", json=PLATFORM_BODY)

    assert response.status_code == 500
    body = response.text
    assert "Traceback" not in body
    assert "model client exploded" not in body, "no internals in the response body"
    assert response.json() == {"detail": "internal error handling query"}


def test_healthz_is_ok_and_does_not_touch_the_agent(
    client: TestClient, agent: StubAgent
) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert agent.calls == []


def test_set_up_runs_once_at_startup(agent: StubAgent) -> None:
    with TestClient(create_app(agent)) as client:
        assert agent.set_up_calls == 1, "lifespan startup must warm the agent"
        client.post("/query", json=PLATFORM_BODY)
        client.post("/query", json=PLATFORM_BODY)

    assert agent.set_up_calls == 1
    assert len(agent.calls) == 2


def test_set_up_runs_lazily_when_lifespan_is_skipped(
    client: TestClient, agent: StubAgent
) -> None:
    # `TestClient` outside a context manager never fires startup; the first
    # request must still get a set-up agent.
    assert agent.set_up_calls == 0

    client.post("/query", json=PLATFORM_BODY)
    client.post("/query", json=PLATFORM_BODY)

    assert agent.set_up_calls == 1


def test_status_mirrors_healthz_for_external_callers() -> None:
    """Cloud Run's front end swallows /healthz on the public URL, so /status is
    the endpoint an operator (or an uptime check) can actually reach."""
    client = TestClient(create_app(agent=StubAgent()))
    healthz = client.get("/healthz")
    status = client.get("/status")
    assert healthz.status_code == status.status_code == 200
    assert healthz.json() == status.json() == {"status": "ok"}
