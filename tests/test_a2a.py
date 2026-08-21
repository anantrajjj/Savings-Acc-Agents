"""Tests for the A01 A2A surface, in both wire dialects.

Everything here runs against a stub agent injected into `create_app`, so no
credentials, no network and no model call is involved. The dialect fixtures are
the point of the file: the same handler must answer a v0.3 caller in v0.3 and a
v1.0 caller in v1.0, down to the enum casing.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from savings_flow.agents.a01_id_verification.a2a import (
    AGENT_CARD_PATH,
    A2A_PATH,
    Dialect,
    build_agent_card,
    build_agent_card_v03,
    build_agent_card_v1,
    dialect_of,
    extract_pan_and_name,
)
from savings_flow.agents.a01_id_verification.contract import A01Output, Explain
from savings_flow.agents.a01_id_verification.service import create_app

PAN = "ZZBPS1002B"
FULL_NAME = "R. K. Sharma"
REGISTERED_NAME = "Rajesh Kumar Sharma"

# A real, contract-valid output. The data part must carry this dict untouched —
# downstream agents consume it, so any reshaping here is a bug.
OUTPUT: dict[str, Any] = A01Output(
    verified=True,
    nameMatchScore=0.93,
    registeredName=REGISTERED_NAME,
    explain=Explain(
        reasoning_summary="PAN is live and the applicant name matches the record.",
        evidence_refs=[f"mock_pan_registry:{PAN}"],
        policy_citations=["RBI Master Direction KYC 2016 s.16"],
        confidence=0.93,
        deciding_factors=["pan_status=ACTIVE", "name_match=0.93"],
    ),
).to_payload()

FIXED_NOW = datetime(2026, 8, 21, 9, 30, 15, tzinfo=UTC)
FIXED_TIMESTAMP = "2026-08-21T09:30:15Z"


class StubAgent:
    """Records how it was called; never touches a model."""

    def __init__(self, error: Exception | None = None) -> None:
        self.set_up_calls = 0
        self.calls: list[dict[str, Any]] = []
        self._error = error

    def set_up(self) -> None:
        self.set_up_calls += 1

    def query(self, **kwargs: Any) -> dict[str, Any]:
        # Captured as **kwargs so a test can assert the keyword *name* used.
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return dict(OUTPUT)


class CountingIds:
    """`kind-N` ids, so a payload's ids are readable and reproducible."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def __call__(self, kind: str) -> str:
        self.counts[kind] += 1
        return f"{kind}-{self.counts[kind]}"


def build_client(agent: StubAgent) -> TestClient:
    return TestClient(
        create_app(agent, a2a_new_id=CountingIds(), a2a_now=lambda: FIXED_NOW),
        raise_server_exceptions=False,
    )


# ----------------------------------------------------------------------
# Dialect fixtures
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class Wire:
    """How one dialect spells the things this surface sends and accepts."""

    label: str
    send: str
    stream: str
    user_role: str
    agent_role: str
    working: str
    completed: str
    input_required: str
    failed: str

    def text_part(self, text: str) -> dict[str, Any]:
        part: dict[str, Any] = {"text": text}
        if self.label == "0.3":
            part["kind"] = "text"
        return part

    def data_part(self, data: dict[str, Any]) -> dict[str, Any]:
        part: dict[str, Any] = {"data": data}
        if self.label == "0.3":
            part["kind"] = "data"
        return part

    def task(self, result: dict[str, Any]) -> dict[str, Any]:
        # v1.0 wraps the Task in the oneof member it came from.
        return result if self.label == "0.3" else result["task"]


V03 = Wire(
    label="0.3",
    send="message/send",
    stream="message/stream",
    user_role="user",
    agent_role="agent",
    working="working",
    completed="completed",
    input_required="input-required",
    failed="failed",
)
V1 = Wire(
    label="1.0",
    send="SendMessage",
    stream="SendStreamingMessage",
    user_role="ROLE_USER",
    agent_role="ROLE_AGENT",
    working="TASK_STATE_WORKING",
    completed="TASK_STATE_COMPLETED",
    input_required="TASK_STATE_INPUT_REQUIRED",
    failed="TASK_STATE_FAILED",
)


@pytest.fixture(params=[V03, V1], ids=["v0.3", "v1.0"])
def wire(request: pytest.FixtureRequest) -> Wire:
    return request.param


@pytest.fixture
def agent() -> StubAgent:
    return StubAgent()


@pytest.fixture
def client(agent: StubAgent) -> TestClient:
    return build_client(agent)


def rpc(
    method: str,
    parts: list[dict[str, Any]],
    *,
    wire: Wire,
    request_id: Any = 1,
    context_id: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": wire.user_role,
        "messageId": "caller-message-1",
        "parts": parts,
    }
    if context_id is not None:
        message["contextId"] = context_id
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {"message": message},
    }


def sse_events(response: Response) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def result_of(response: Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert "error" not in body, body
    return body["result"]


# ----------------------------------------------------------------------
# Agent card
# ----------------------------------------------------------------------
def test_well_known_serves_the_v1_card(client: TestClient) -> None:
    response = client.get(AGENT_CARD_PATH)

    assert response.status_code == 200
    card = response.json()
    # v1.0 moved the endpoint into supportedInterfaces and dropped the
    # top-level url/protocolVersion pair entirely.
    assert "url" not in card
    assert "protocolVersion" not in card
    interfaces = card["supportedInterfaces"]
    assert [interface["protocolVersion"] for interface in interfaces] == ["1.0", "0.3"]
    for interface in interfaces:
        # Upper case per specification/a2a.proto: "The core ones officially
        # supported are `JSONRPC`, `GRPC` and `HTTP+JSON`."
        assert interface["protocolBinding"] == "JSONRPC"
        assert interface["url"].endswith(A2A_PATH)

    assert card["name"]
    assert card["version"] == "1.0.0"
    assert card["capabilities"] == {"streaming": True}
    assert card["defaultInputModes"] == ["text/plain", "application/json"]
    assert card["defaultOutputModes"] == ["text/plain", "application/json"]
    assert len(card["skills"]) == 1
    skill = card["skills"][0]
    assert skill["id"] == "verify_identity"
    assert skill["name"] and skill["description"] and skill["tags"]
    assert len(skill["examples"]) == 2


def test_v1_card_description_states_what_the_agent_decides(client: TestClient) -> None:
    # This text is what the Gemini Enterprise assistant routes on, so it has to
    # name the input, the verdict and the audit block.
    description = client.get(AGENT_CARD_PATH).json()["description"].lower()

    assert "pan" in description
    assert "name" in description
    assert "explain" in description


def test_well_known_serves_the_v03_card_on_request(client: TestClient) -> None:
    response = client.get(AGENT_CARD_PATH, params={"dialect": "0.3"})

    assert response.status_code == 200
    card = response.json()
    assert card["protocolVersion"] == "0.3"
    assert card["url"].endswith(A2A_PATH)
    assert "supportedInterfaces" not in card
    assert len(card["skills"]) == 1
    assert card["defaultInputModes"] == ["text/plain", "application/json"]
    assert card["defaultOutputModes"] == ["text/plain", "application/json"]


def test_unknown_card_dialect_is_rejected(client: TestClient) -> None:
    response = client.get(AGENT_CARD_PATH, params={"dialect": "0.9"})

    assert response.status_code == 400


def test_card_builders_normalise_a_trailing_slash() -> None:
    base = "https://a01.example.run.app/"

    assert build_agent_card_v03(base)["url"] == "https://a01.example.run.app/a2a"
    assert (
        build_agent_card_v1(base)["supportedInterfaces"][0]["url"]
        == "https://a01.example.run.app/a2a"
    )
    # The dispatcher defaults to the latest dialect.
    assert build_agent_card(base) == build_agent_card_v1(base)
    assert build_agent_card(base, Dialect.V03) == build_agent_card_v03(base)


# ----------------------------------------------------------------------
# message/send — SendMessage
# ----------------------------------------------------------------------
def test_send_with_a_data_part_returns_a_completed_task(
    client: TestClient, agent: StubAgent, wire: Wire
) -> None:
    body = rpc(
        wire.send,
        [wire.data_part({"pan": PAN, "fullName": FULL_NAME})],
        wire=wire,
    )

    task = wire.task(result_of(client.post(A2A_PATH, json=body)))

    assert task["status"]["state"] == wire.completed
    assert task["status"]["timestamp"] == FIXED_TIMESTAMP
    assert task["id"]
    assert task["contextId"]
    assert task["status"]["message"]["role"] == wire.agent_role

    artifacts = task["artifacts"]
    assert len(artifacts) == 1
    assert artifacts[0]["name"] == "a01-identity-verification"
    text_part, data_part = artifacts[0]["parts"]
    # The data part is the contract dict, byte for byte.
    assert data_part["data"] == OUTPUT
    assert OUTPUT["explain"]["reasoning_summary"] in text_part["text"]
    assert "Identity verified" in text_part["text"]

    # History is the inbound user message plus the agent's answer.
    history = task["history"]
    assert len(history) == 2
    assert history[0]["role"] == wire.user_role
    assert history[0]["messageId"] == "caller-message-1"
    assert history[1]["role"] == wire.agent_role

    assert agent.calls == [{"input": {"pan": PAN, "fullName": FULL_NAME}}]


def test_v1_parts_carry_no_kind_and_v03_parts_do(
    client: TestClient, wire: Wire
) -> None:
    body = rpc(wire.send, [wire.data_part({"pan": PAN, "name": FULL_NAME})], wire=wire)

    task = wire.task(result_of(client.post(A2A_PATH, json=body)))
    parts = task["artifacts"][0]["parts"]

    if wire.label == "0.3":
        assert [part["kind"] for part in parts] == ["text", "data"]
        assert task["kind"] == "task"
        assert task["status"]["message"]["kind"] == "message"
    else:
        assert all("kind" not in part for part in parts)
        assert "kind" not in task
        assert "kind" not in task["status"]["message"]


@pytest.mark.parametrize(
    "name_key", ["fullName", "full_name", "name"], ids=["camel", "snake", "bare"]
)
def test_data_part_name_aliases_are_accepted(
    client: TestClient, agent: StubAgent, name_key: str
) -> None:
    body = rpc(V1.send, [{"data": {"pan": PAN, name_key: FULL_NAME}}], wire=V1)

    result_of(client.post(A2A_PATH, json=body))

    assert agent.calls == [{"input": {"pan": PAN, "fullName": FULL_NAME}}]


def test_data_part_is_accepted_without_a_kind_discriminator(
    client: TestClient, agent: StubAgent
) -> None:
    # A v0.3-named call whose parts are unlabelled (or mislabelled) is still
    # served: `kind` is a hint, not a gate.
    body = rpc(
        V03.send,
        [{"kind": "something-else", "data": {"pan": PAN, "fullName": FULL_NAME}}],
        wire=V03,
    )

    result_of(client.post(A2A_PATH, json=body))

    assert agent.calls == [{"input": {"pan": PAN, "fullName": FULL_NAME}}]


def test_send_with_a_text_part_extracts_pan_and_name(
    client: TestClient, agent: StubAgent, wire: Wire
) -> None:
    body = rpc(
        wire.send,
        [wire.text_part(f"Verify PAN {PAN} for {FULL_NAME}")],
        wire=wire,
    )

    task = wire.task(result_of(client.post(A2A_PATH, json=body)))

    assert task["status"]["state"] == wire.completed
    assert agent.calls == [{"input": {"pan": PAN, "fullName": FULL_NAME}}]


@pytest.mark.parametrize(
    ("text", "missing", "present"),
    [
        (f"Verify PAN {PAN}", "fullName", "pan"),
        (f"Please verify the identity of {FULL_NAME}", "pan", "fullName"),
    ],
    ids=["pan-without-name", "name-without-pan"],
)
def test_unusable_text_asks_for_input_instead_of_guessing(
    client: TestClient,
    agent: StubAgent,
    wire: Wire,
    text: str,
    missing: str,
    present: str,
) -> None:
    body = rpc(wire.send, [wire.text_part(text)], wire=wire)

    task = wire.task(result_of(client.post(A2A_PATH, json=body)))

    assert task["status"]["state"] == wire.input_required
    message = task["status"]["message"]["parts"][0]["text"]
    assert missing in message
    assert present not in message.split(". ")[0], (
        "the prompt must name only what is actually missing"
    )
    assert "artifacts" not in task, "no verdict means no artifact"
    assert agent.calls == [], "the agent must not be called on a guess"


def test_context_id_is_echoed_when_supplied(client: TestClient, wire: Wire) -> None:
    body = rpc(
        wire.send,
        [wire.data_part({"pan": PAN, "fullName": FULL_NAME})],
        wire=wire,
        context_id="caller-context-xyz",
    )

    task = wire.task(result_of(client.post(A2A_PATH, json=body)))

    assert task["contextId"] == "caller-context-xyz"
    assert task["status"]["message"]["contextId"] == "caller-context-xyz"


def test_context_id_is_generated_when_absent(client: TestClient, wire: Wire) -> None:
    body = rpc(wire.send, [wire.data_part({"pan": PAN, "fullName": FULL_NAME})], wire=wire)

    task = wire.task(result_of(client.post(A2A_PATH, json=body)))

    # Injected id factory, so the generated value is known exactly.
    assert task["contextId"] == "context-1"
    assert task["id"] == "task-1"


# ----------------------------------------------------------------------
# JSON-RPC error handling
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "method",
    ["message/subscribe", "GetTask", "CancelTask", "", "SENDMESSAGE"],
)
def test_unknown_method_is_a_method_not_found_error(
    client: TestClient, agent: StubAgent, method: str
) -> None:
    body = rpc(method, [{"data": {"pan": PAN, "fullName": FULL_NAME}}], wire=V1)
    body["id"] = "req-77"

    response = client.post(A2A_PATH, json=body)

    assert response.status_code == 200, "a JSON-RPC error is still a successful POST"
    payload = response.json()
    assert payload["id"] == "req-77"
    assert payload["error"]["code"] == -32601
    assert "result" not in payload
    assert agent.calls == []


def test_malformed_json_is_a_parse_error_echoing_the_id(client: TestClient) -> None:
    broken = '{"jsonrpc": "2.0", "id": "req-88", "method": "SendMessage", "params":'

    response = client.post(
        A2A_PATH, content=broken, headers={"Content-Type": "application/json"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["error"]["code"] == -32700
    assert payload["id"] == "req-88"


def test_unparseable_body_without_an_id_reports_a_null_id(client: TestClient) -> None:
    response = client.post(
        A2A_PATH, content="not json at all", headers={"Content-Type": "application/json"}
    )

    payload = response.json()
    assert payload["error"]["code"] == -32700
    assert payload["id"] is None


@pytest.mark.parametrize(
    "params",
    [None, {}, {"message": None}, {"message": {"role": "ROLE_USER"}}],
    ids=["no-params", "empty-params", "null-message", "message-without-parts"],
)
def test_bad_params_is_an_invalid_params_error(
    client: TestClient, agent: StubAgent, params: Any
) -> None:
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": 42,
        "method": "SendMessage",
    }
    if params is not None:
        body["params"] = params

    response = client.post(A2A_PATH, json=body)

    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == 42
    assert payload["error"]["code"] == -32602
    assert agent.calls == []


def test_a_json_array_body_is_an_invalid_request(client: TestClient) -> None:
    response = client.post(A2A_PATH, json=[{"jsonrpc": "2.0"}])

    assert response.json()["error"]["code"] == -32600


# ----------------------------------------------------------------------
# Streaming
# ----------------------------------------------------------------------
def test_stream_emits_working_then_artifact_then_final_status(
    client: TestClient, wire: Wire
) -> None:
    body = rpc(
        wire.stream,
        [wire.data_part({"pan": PAN, "fullName": FULL_NAME})],
        wire=wire,
        request_id="stream-1",
    )

    response = client.post(A2A_PATH, json=body)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = sse_events(response)
    assert len(events) == 3
    assert [event["id"] for event in events] == ["stream-1"] * 3

    first, second, third = (event["result"] for event in events)

    # 1. the task, working
    working = wire.task(first)
    assert working["status"]["state"] == wire.working
    assert "artifacts" not in working
    task_id, context_id = working["id"], working["contextId"]

    # 2. the artifact
    artifact_event = second if wire.label == "0.3" else second["artifactUpdate"]
    assert artifact_event["taskId"] == task_id
    assert artifact_event["contextId"] == context_id
    artifact = artifact_event["artifact"]
    assert artifact["name"] == "a01-identity-verification"
    assert artifact["parts"][1]["data"] == OUTPUT

    # 3. the terminal status
    status_event = third if wire.label == "0.3" else third["statusUpdate"]
    assert status_event["taskId"] == task_id
    assert status_event["contextId"] == context_id
    assert status_event["status"]["state"] == wire.completed
    # The final event repeats the verdict, so a client that ignored the
    # artifact event still ends up with the contract dict.
    assert status_event["status"]["message"]["parts"][1]["data"] == OUTPUT


def test_v03_stream_events_use_kind_and_final(client: TestClient) -> None:
    body = rpc(V03.stream, [V03.data_part({"pan": PAN, "fullName": FULL_NAME})], wire=V03)

    events = [event["result"] for event in sse_events(client.post(A2A_PATH, json=body))]

    assert [event["kind"] for event in events] == [
        "task",
        "artifact-update",
        "status-update",
    ]
    assert events[-1]["final"] is True


def test_v1_stream_events_use_oneof_members_and_no_final(client: TestClient) -> None:
    body = rpc(V1.stream, [V1.data_part({"pan": PAN, "fullName": FULL_NAME})], wire=V1)

    events = [event["result"] for event in sse_events(client.post(A2A_PATH, json=body))]

    assert [next(iter(event)) for event in events] == [
        "task",
        "artifactUpdate",
        "statusUpdate",
    ]
    # v1.0 removed `final`; the closed stream and the terminal state say it.
    assert all("final" not in json.dumps(event) for event in events)
    assert events[-1]["statusUpdate"]["status"]["state"] == "TASK_STATE_COMPLETED"


def test_stream_without_a_usable_input_ends_in_input_required(
    client: TestClient, agent: StubAgent, wire: Wire
) -> None:
    body = rpc(wire.stream, [wire.text_part("verify this applicant")], wire=wire)

    events = [event["result"] for event in sse_events(client.post(A2A_PATH, json=body))]

    assert len(events) == 2, "no artifact without a verdict"
    last = events[-1] if wire.label == "0.3" else events[-1]["statusUpdate"]
    assert last["status"]["state"] == wire.input_required
    assert agent.calls == []


# ----------------------------------------------------------------------
# Failure path
# ----------------------------------------------------------------------
def test_agent_exception_becomes_a_failed_task_without_internals(wire: Wire) -> None:
    boom = RuntimeError("model client exploded at /secret/path/agent.py:42")
    client = build_client(StubAgent(error=boom))
    body = rpc(wire.send, [wire.data_part({"pan": PAN, "fullName": FULL_NAME})], wire=wire)

    response = client.post(A2A_PATH, json=body)

    assert response.status_code == 200
    task = wire.task(result_of(response))
    assert task["status"]["state"] == wire.failed
    assert "artifacts" not in task
    assert "Traceback" not in response.text
    assert "model client exploded" not in response.text
    assert "/secret/path" not in response.text


def test_stream_failure_still_terminates(wire: Wire) -> None:
    client = build_client(StubAgent(error=RuntimeError("boom")))
    body = rpc(wire.stream, [wire.data_part({"pan": PAN, "fullName": FULL_NAME})], wire=wire)

    events = [event["result"] for event in sse_events(client.post(A2A_PATH, json=body))]

    last = events[-1] if wire.label == "0.3" else events[-1]["statusUpdate"]
    assert last["status"]["state"] == wire.failed


# ----------------------------------------------------------------------
# Unit-level helpers
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"Verify PAN {PAN} for {FULL_NAME}", (PAN, FULL_NAME)),
        (f"pan: {PAN.lower()}, name: {FULL_NAME}", (PAN, FULL_NAME)),
        (f"{FULL_NAME} ({PAN})", (PAN, FULL_NAME)),
        (f"Check {PAN}", (PAN, None)),
        ("Verify the applicant", (None, None)),
        ("identity of Anjali Deshpande", (None, "Anjali Deshpande")),
    ],
)
def test_text_extraction(text: str, expected: tuple[str | None, str | None]) -> None:
    assert extract_pan_and_name(text) == expected


def test_extraction_keeps_trailing_prose_which_is_the_documented_limit() -> None:
    # Honest about the heuristic: prose after the name is kept, because nothing
    # here parses grammar. A caller that needs precision sends a data part.
    _, name = extract_pan_and_name(f"Verify PAN {PAN} for {FULL_NAME} who applied today")

    assert name == "R. K. Sharma who applied today"


@pytest.mark.parametrize(
    ("method", "role", "expected"),
    [
        ("SendMessage", "user", Dialect.V1),
        ("message/send", "ROLE_USER", Dialect.V03),
        (None, "ROLE_USER", Dialect.V1),
        (None, "user", Dialect.V03),
        (None, None, Dialect.V1),
    ],
)
def test_dialect_detection_prefers_the_method_name(
    method: str | None, role: str | None, expected: Dialect
) -> None:
    message: dict[str, Any] = {"role": role} if role is not None else {}

    assert dialect_of(method, message) == expected


# ----------------------------------------------------------------------
# Regression: the pre-existing surface is untouched
# ----------------------------------------------------------------------
def test_platform_query_envelope_still_works(
    client: TestClient, agent: StubAgent
) -> None:
    response = client.post(
        "/query",
        json={"class_method": "query", "input": {"input": {"pan": PAN, "fullName": FULL_NAME}}},
    )

    assert response.status_code == 200
    assert response.json() == {"output": OUTPUT}
    assert agent.calls == [{"input": {"pan": PAN, "fullName": FULL_NAME}}]


def test_health_endpoints_still_answer(client: TestClient, agent: StubAgent) -> None:
    for path in ("/healthz", "/status"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
    assert agent.calls == []
