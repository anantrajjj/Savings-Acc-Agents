"""A2A (Agent2Agent) protocol surface for A01 — Identity Verification Agent.

Gemini Enterprise can register this service as a "Custom agent via A2A": it is
handed an agent card (pasted inline in the console) and then speaks JSON-RPC 2.0
at the card's endpoint. That is a different wire language from the platform
`POST /query` envelope this service already serves, so this module is a
translation shell — it parses A2A messages, calls `A01Agent.query(input=...)`
unchanged, and dresses the contract dict back up as an A2A Task.

**Two dialects, one brain.** A2A v1.0 renamed the operations (`message/send` →
`SendMessage`), dropped the `kind` discriminator from Part and from the stream
events, moved enums to SCREAMING_SNAKE_CASE, and replaced the card's single
`url` with a `supportedInterfaces` array. Google's Agent Registry still speaks
v0.3 alongside v1.0, so this surface accepts both and answers in whichever
dialect the request arrived in. Everything between the parser and the renderer
is dialect-neutral (`_Task`, `_Message`, `_Part` below); the dialect exists only
at the two edges.

Why hand-rolled rather than `a2a-sdk`: the agent is a plain class, not an ADK
`LlmAgent`, so neither `google.adk.a2a` nor the SDK's server helpers fit it
without inverting the agent's own design. Two methods and one skill is less code
than the adaptation would be — and a hand-rolled renderer is what makes serving
both dialects from one implementation cheap.

Shapes follow the published v1.0 and v0.3 specs. They have not yet been
exercised against a real Gemini Enterprise call — see the doc's caveat.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Protocol

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

_LOGGER = logging.getLogger(__name__)

AGENT_NAME = "ID Verification Agent"
AGENT_VERSION = "1.0.0"
SKILL_ID = "verify_identity"
ARTIFACT_NAME = "a01-identity-verification"

A2A_PATH = "/a2a"
AGENT_CARD_PATH = "/.well-known/agent-card.json"

# The card's `protocolBinding` for the JSON-RPC binding. Google's tooling uses
# The normative specification/a2a.proto states the officially supported bindings
# are `JSONRPC`, `GRPC` and `HTTP+JSON` — upper case. Only JSON-RPC is served
# here. (Google's Agent Registry gcloud surface spells its own binding values
# lower case, e.g. `http-json`; that is the registry's flavour, not the card's.)
JSONRPC_BINDING = "JSONRPC"

JSONRPC_VERSION = "2.0"
# JSON-RPC 2.0 reserved error codes. These travel as HTTP 200 with an `error`
# member: a transport-level status would hide the code from the caller.
ERROR_PARSE = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602

# The agent's two required inputs, named as the contract names them so an
# input-required message tells the caller exactly which key to supply.
FIELD_PAN = "pan"
FIELD_FULL_NAME = "fullName"


class Dialect(Enum):
    """Which generation of the A2A wire format a peer speaks."""

    V03 = "0.3"
    V1 = "1.0"


LATEST = Dialect.V1

# method name -> (dialect, streaming?)
_METHODS: dict[str, tuple[Dialect, bool]] = {
    "SendMessage": (Dialect.V1, False),
    "SendStreamingMessage": (Dialect.V1, True),
    "message/send": (Dialect.V03, False),
    "message/stream": (Dialect.V03, True),
}

# Neutral state names (v0.3 spelling doubles as the internal vocabulary).
STATE_WORKING = "working"
STATE_COMPLETED = "completed"
STATE_INPUT_REQUIRED = "input-required"
STATE_FAILED = "failed"

_V1_STATES = {
    STATE_WORKING: "TASK_STATE_WORKING",
    STATE_COMPLETED: "TASK_STATE_COMPLETED",
    STATE_INPUT_REQUIRED: "TASK_STATE_INPUT_REQUIRED",
    STATE_FAILED: "TASK_STATE_FAILED",
}
_V1_ROLE_AGENT = "ROLE_AGENT"
_V03_ROLE_AGENT = "agent"


# ----------------------------------------------------------------------
# Text extraction
# ----------------------------------------------------------------------
# Case-insensitive PAN shape: five letters, four digits, one letter. Anchored on
# word boundaries so it cannot bite a chunk out of a longer alphanumeric token.
_PAN_IN_TEXT = re.compile(r"\b([A-Za-z]{5}[0-9]{4}[A-Za-z])\b")

# Label and filler words a caller wraps a name in when they type a sentence
# instead of sending a data part. Deliberately short: every entry is a word that
# cannot be part of an applicant's name. This is a filter, not a parser.
_FILLER_WORDS = frozenset(
    {
        "a",
        "against",
        "and",
        "applicant",
        "card",
        "check",
        "customer",
        "for",
        "full",
        "fullname",
        "identity",
        "is",
        "kyc",
        "my",
        "name",
        "no",
        "number",
        "of",
        "pan",
        "please",
        "the",
        "verification",
        "verify",
        "with",
    }
)

# Stripped from token edges before the filler test and from the kept token. `.`
# survives inside a kept token so "R. K. Sharma" stays intact.
_EDGE_PUNCTUATION = ",;:!?()[]{}\"'"


class _AgentLike(Protocol):
    """The slice of the agent this layer depends on."""

    def query(self, *, input: dict[str, Any]) -> dict[str, Any]: ...


IdFactory = Callable[[str], str]
Clock = Callable[[], datetime]


def _uuid_id(kind: str) -> str:
    """Default id factory. `kind` is a hint an injected test double can use."""
    return f"{kind}-{uuid.uuid4()}" if kind else str(uuid.uuid4())


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    """ISO-8601 with a `Z` suffix — how A2A spells timestamps in both dialects."""
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ----------------------------------------------------------------------
# Agent card
# ----------------------------------------------------------------------
_DESCRIPTION = (
    "Verifies an Indian PAN (Permanent Account Number) and matches the name the "
    "applicant declared against the name registered to that PAN. Returns a "
    "structured verdict — verified true/false, a 0.0-1.0 name match score and the "
    "registered name — together with an audit 'explain' block carrying the "
    "reasoning summary, evidence references, policy citations, confidence and "
    "deciding factors. Use it for the identity-verification step of savings "
    "account opening (BFSI KYC). It does not screen sanctions or PEP lists, read "
    "identity documents, match faces, or verify an address."
)

_SKILL_DESCRIPTION = (
    "Given a PAN and the applicant's declared full name, validates the PAN's "
    "structure, looks the PAN up in the PAN registry, and adjudicates whether the "
    "declared name matches the registered name (tolerating initials, honorifics, "
    "surname-first ordering and transliteration variants). Answers with the "
    "verdict, the match score, the registered name and the audit explain block. "
    "Accepts either a structured data part carrying 'pan' and 'fullName', or a "
    "plain sentence naming both."
)

_SKILL_TAGS = ["kyc", "identity-verification", "pan", "bfsi", "onboarding", "india"]


def _card_common(base_url: str) -> dict[str, Any]:
    """The card fields both dialects spell identically."""
    return {
        "name": AGENT_NAME,
        "description": _DESCRIPTION,
        "version": AGENT_VERSION,
        "capabilities": {"streaming": True},
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {
                "id": SKILL_ID,
                "name": "Verify PAN and applicant name",
                "description": _SKILL_DESCRIPTION,
                "tags": list(_SKILL_TAGS),
                "examples": [
                    # Structured form: what another agent should send. Shown
                    # without a `kind` because v1.0 dropped it and this surface
                    # accepts the part either way.
                    '{"data": {"pan": "ZZBPS1002B", "fullName": "R. K. Sharma"}}',
                    # Natural-language form: what a person types in the assistant.
                    "Verify PAN ZZBPS1002B for R. K. Sharma",
                ],
            }
        ],
    }


def build_agent_card_v1(base_url: str) -> dict[str, Any]:
    """The v1.0-shaped card: endpoints live in `supportedInterfaces`.

    Both dialects are advertised at the same URL because the endpoint really
    does serve both, and a v0.3 client reading a v1.0 card should be able to
    find an interface it can drive.
    """
    root = base_url.rstrip("/")
    return {
        "supportedInterfaces": [
            {
                "url": f"{root}{A2A_PATH}",
                "protocolBinding": JSONRPC_BINDING,
                "protocolVersion": version,
            }
            for version in (Dialect.V1.value, Dialect.V03.value)
        ],
        **_card_common(base_url),
    }


def build_agent_card_v03(base_url: str) -> dict[str, Any]:
    """The v0.3-shaped card: one top-level `url` and `protocolVersion`.

    Kept because a console field or registry that predates v1.0 will reject the
    `supportedInterfaces` form outright, and pasting a card is a one-shot action
    with no error message worth reading.
    """
    root = base_url.rstrip("/")
    return {
        "protocolVersion": Dialect.V03.value,
        "url": f"{root}{A2A_PATH}",
        **_card_common(base_url),
    }


def build_agent_card(base_url: str, dialect: Dialect = LATEST) -> dict[str, Any]:
    """Dispatch to the dialect-specific card builder."""
    if dialect is Dialect.V03:
        return build_agent_card_v03(base_url)
    return build_agent_card_v1(base_url)


# ----------------------------------------------------------------------
# Dialect-neutral internal representation
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class _Part:
    """One message/artifact part. Exactly one of `text` / `data` is set."""

    text: str | None = None
    data: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class _Message:
    message_id: str
    task_id: str
    context_id: str
    parts: tuple[_Part, ...]


@dataclass(frozen=True)
class _Artifact:
    artifact_id: str
    name: str
    parts: tuple[_Part, ...]


@dataclass(frozen=True)
class _Status:
    state: str
    timestamp: str
    message: _Message | None = None


@dataclass(frozen=True)
class _Task:
    task_id: str
    context_id: str
    status: _Status
    artifacts: tuple[_Artifact, ...] = ()
    # Raw inbound messages are echoed verbatim: they arrived in the caller's own
    # dialect, which is the dialect we are answering in.
    history: tuple[Mapping[str, Any] | _Message, ...] = ()


@dataclass(frozen=True)
class _TaskEvent:
    task: _Task


@dataclass(frozen=True)
class _ArtifactEvent:
    task_id: str
    context_id: str
    artifact: _Artifact
    index: int = 0


@dataclass(frozen=True)
class _StatusEvent:
    task_id: str
    context_id: str
    status: _Status
    final: bool = False


@dataclass(frozen=True)
class _Outcome:
    """What the agent (or the refusal to call it) produced."""

    state: str
    parts: tuple[_Part, ...]
    # None whenever there is no verdict to publish, i.e. every non-completed
    # state — which is exactly when no artifact should be emitted.
    output: dict[str, Any] | None = None


# ----------------------------------------------------------------------
# Rendering (neutral -> wire)
# ----------------------------------------------------------------------
def _render_part(part: _Part, dialect: Dialect) -> dict[str, Any]:
    if part.data is not None:
        if dialect is Dialect.V03:
            return {"kind": "data", "data": dict(part.data)}
        return {"data": dict(part.data)}
    text = part.text or ""
    if dialect is Dialect.V03:
        return {"kind": "text", "text": text}
    return {"text": text}


def _render_message(message: _Message, dialect: Dialect) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "role": _V03_ROLE_AGENT if dialect is Dialect.V03 else _V1_ROLE_AGENT,
        "messageId": message.message_id,
        "taskId": message.task_id,
        "contextId": message.context_id,
        "parts": [_render_part(part, dialect) for part in message.parts],
    }
    if dialect is Dialect.V03:
        # v1.0 discriminates by the enclosing member instead.
        rendered["kind"] = "message"
    return rendered


def _render_artifact(artifact: _Artifact, dialect: Dialect) -> dict[str, Any]:
    return {
        "artifactId": artifact.artifact_id,
        "name": artifact.name,
        "parts": [_render_part(part, dialect) for part in artifact.parts],
    }


def _render_status(status: _Status, dialect: Dialect) -> dict[str, Any]:
    state = status.state if dialect is Dialect.V03 else _V1_STATES[status.state]
    rendered: dict[str, Any] = {"state": state, "timestamp": status.timestamp}
    if status.message is not None:
        rendered["message"] = _render_message(status.message, dialect)
    return rendered


def _render_history(
    entry: Mapping[str, Any] | _Message, dialect: Dialect
) -> dict[str, Any]:
    if isinstance(entry, _Message):
        return _render_message(entry, dialect)
    return dict(entry)


def _render_task(task: _Task, dialect: Dialect) -> dict[str, Any]:
    rendered: dict[str, Any] = {"id": task.task_id, "contextId": task.context_id}
    if dialect is Dialect.V03:
        rendered["kind"] = "task"
    rendered["status"] = _render_status(task.status, dialect)
    if task.artifacts:
        rendered["artifacts"] = [
            _render_artifact(artifact, dialect) for artifact in task.artifacts
        ]
    if task.history:
        rendered["history"] = [
            _render_history(entry, dialect) for entry in task.history
        ]
    return rendered


def _render_send_result(task: _Task, dialect: Dialect) -> dict[str, Any]:
    rendered = _render_task(task, dialect)
    # v1.0 wraps the result in the oneof member it came from; v0.3 returns the
    # Task bare and relies on its `kind`.
    return rendered if dialect is Dialect.V03 else {"task": rendered}


def _render_event(
    event: _TaskEvent | _ArtifactEvent | _StatusEvent, dialect: Dialect
) -> dict[str, Any]:
    if isinstance(event, _TaskEvent):
        return _render_send_result(event.task, dialect)
    if isinstance(event, _ArtifactEvent):
        if dialect is Dialect.V03:
            return {
                "kind": "artifact-update",
                "taskId": event.task_id,
                "contextId": event.context_id,
                "artifact": _render_artifact(event.artifact, dialect),
                "append": False,
                "lastChunk": True,
            }
        return {
            "artifactUpdate": {
                "taskId": event.task_id,
                "contextId": event.context_id,
                "artifact": _render_artifact(event.artifact, dialect),
                "index": event.index,
            }
        }
    if dialect is Dialect.V03:
        return {
            "kind": "status-update",
            "taskId": event.task_id,
            "contextId": event.context_id,
            "status": _render_status(event.status, dialect),
            "final": event.final,
        }
    # v1.0 removed `final`: the terminal state plus the closed stream say it.
    return {
        "statusUpdate": {
            "taskId": event.task_id,
            "contextId": event.context_id,
            "status": _render_status(event.status, dialect),
        }
    }


# ----------------------------------------------------------------------
# Inbound parsing (wire -> neutral)
# ----------------------------------------------------------------------
def _first_string(source: Mapping[str, Any], *keys: str) -> str | None:
    """First key present with a non-blank string value."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def dialect_of(method: str | None, message: Mapping[str, Any]) -> Dialect:
    """Which dialect to answer in.

    The method name is decisive — the two generations use disjoint names, and a
    caller that asks for `SendMessage` can parse v1.0 whatever else it wrote.
    The message shape is a fallback for a method name that pins nothing (the
    served routes always pin one; `SCREAMING_SNAKE` role casing is the signal),
    and the latest dialect is the last resort.
    """
    route = _METHODS.get(method or "")
    if route is not None:
        return route[0]
    role = message.get("role")
    if isinstance(role, str):
        return Dialect.V1 if role.upper().startswith("ROLE_") else Dialect.V03
    return LATEST


def _data_parts(parts: Sequence[Any]) -> Iterable[Mapping[str, Any]]:
    # `kind` is a hint, never a gate: v1.0 removed it, and a v0.3 caller that
    # mislabels a part still gets served.
    for part in parts:
        if isinstance(part, Mapping) and isinstance(part.get("data"), Mapping):
            yield part["data"]


def _text_parts(parts: Sequence[Any]) -> Iterable[str]:
    for part in parts:
        if isinstance(part, Mapping) and isinstance(part.get("text"), str):
            yield part["text"]


def extract_pan_and_name(text: str) -> tuple[str | None, str | None]:
    """Pull a PAN and a name out of one free-text part.

    The PAN is a regex match on its fixed structure, which is reliable. The name
    is *not* parsed: the PAN token and a short list of label/filler words are
    removed and whatever is left is taken as the name. That is right for
    "Verify PAN ZZBPS1002B for R. K. Sharma" and wrong for anything carrying
    extra prose ("... for R. K. Sharma who applied yesterday" keeps the tail).
    Callers who need reliability send a data part; the agent's own name matcher
    still owns the verdict, so a sloppy name here degrades to a low score rather
    than a false accept.
    """
    match = _PAN_IN_TEXT.search(text)
    pan = match.group(1).upper() if match else None

    remainder = text[: match.start()] + " " + text[match.end() :] if match else text
    kept: list[str] = []
    for token in remainder.split():
        if token.strip(_EDGE_PUNCTUATION).lower() in _FILLER_WORDS:
            continue
        cleaned = token.strip(_EDGE_PUNCTUATION)
        if cleaned:
            kept.append(cleaned)
    name = " ".join(kept)
    # One stray letter is noise, not a name; two alphabetic characters is the
    # smallest thing worth handing to the matcher (an initial pair, say).
    if sum(character.isalpha() for character in name) < 2:
        name = ""
    return pan, name or None


def extract_agent_input(parts: Sequence[Any]) -> tuple[str | None, str | None]:
    """Extract `(pan, fullName)` from `params.message.parts`.

    Data parts win over text parts: a structured caller has said exactly what it
    means, so its values are never second-guessed by the text heuristic.
    """
    for data in _data_parts(parts):
        return (
            _first_string(data, FIELD_PAN),
            _first_string(data, FIELD_FULL_NAME, "full_name", "name"),
        )

    for text in _text_parts(parts):
        pan, name = extract_pan_and_name(text)
        if pan or name:
            return pan, name
    return None, None


# ----------------------------------------------------------------------
# Composition
# ----------------------------------------------------------------------
def verdict_line(output: Mapping[str, Any]) -> str:
    """Human-readable rendering of the contract dict, for the assistant to show."""
    verified = bool(output.get("verified"))
    headline = "Identity verified" if verified else "Identity NOT verified"
    score = output.get("nameMatchScore")
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        headline += f" (name match score {float(score):.2f}"
        registered = output.get("registeredName")
        headline += f" against registered name '{registered}')" if registered else ")"
    explain = output.get("explain")
    summary = explain.get("reasoning_summary") if isinstance(explain, Mapping) else None
    return f"{headline}. {summary}" if summary else f"{headline}."


class A2AHandler:
    """Turns A2A JSON-RPC calls into `A01Agent.query` calls and back.

    Ids and timestamps come from injected factories so composed payloads are
    reproducible under test; production gets uuid4 and the wall clock.
    """

    def __init__(
        self,
        get_agent: Callable[[], _AgentLike],
        *,
        new_id: IdFactory = _uuid_id,
        now: Clock = _utc_now,
    ) -> None:
        self._get_agent = get_agent
        self._new_id = new_id
        self._now = now

    # -- helpers -------------------------------------------------------
    def _ids(self, message: Mapping[str, Any]) -> tuple[str, str]:
        """Task id (always fresh) and context id (echoed when the caller set one)."""
        context_id = _first_string(message, "contextId") or self._new_id("context")
        return self._new_id("task"), context_id

    def _message(
        self, *, task_id: str, context_id: str, parts: tuple[_Part, ...]
    ) -> _Message:
        return _Message(
            message_id=self._new_id("message"),
            task_id=task_id,
            context_id=context_id,
            parts=parts,
        )

    def _status(self, state: str, message: _Message | None = None) -> _Status:
        return _Status(state=state, timestamp=_iso(self._now()), message=message)

    def _artifact(self, parts: tuple[_Part, ...]) -> _Artifact:
        return _Artifact(
            artifact_id=self._new_id("artifact"), name=ARTIFACT_NAME, parts=parts
        )

    # -- the work ------------------------------------------------------
    async def _outcome(self, parts: Sequence[Any]) -> _Outcome:
        pan, full_name = extract_agent_input(parts)
        missing = [
            name
            for name, value in ((FIELD_PAN, pan), (FIELD_FULL_NAME, full_name))
            if value is None
        ]
        if missing:
            # Guessing a PAN or a name would manufacture KYC evidence, so the
            # agent is not called at all — the caller is asked instead.
            return _Outcome(
                state=STATE_INPUT_REQUIRED,
                parts=(
                    _Part(
                        text=(
                            "Cannot verify identity yet: missing "
                            + " and ".join(missing)
                            + ". Send a data part with "
                            f"{{'{FIELD_PAN}': ..., '{FIELD_FULL_NAME}': ...}} "
                            "or a message naming both."
                        )
                    ),
                ),
            )

        agent_input = {FIELD_PAN: pan, FIELD_FULL_NAME: full_name}
        try:
            # Off the event loop: the agent's model call is synchronous.
            output = await run_in_threadpool(
                lambda: self._get_agent().query(input=agent_input)
            )
        except Exception:
            # The agent is contracted never to raise, so this is defence in
            # depth. The trace goes to the log, never to the caller.
            _LOGGER.exception("A01 agent raised while handling an A2A message")
            return _Outcome(
                state=STATE_FAILED,
                parts=(
                    _Part(text="Identity verification failed due to an internal error."),
                ),
            )

        return _Outcome(
            state=STATE_COMPLETED,
            parts=(_Part(text=verdict_line(output)), _Part(data=output)),
            output=dict(output),
        )

    async def send(self, message: Mapping[str, Any]) -> _Task:
        """Handle SendMessage / message/send: one Task for the whole exchange."""
        task_id, context_id = self._ids(message)
        outcome = await self._outcome(message.get("parts") or [])
        agent_message = self._message(
            task_id=task_id, context_id=context_id, parts=outcome.parts
        )
        artifacts = (
            (self._artifact(outcome.parts),) if outcome.output is not None else ()
        )
        return _Task(
            task_id=task_id,
            context_id=context_id,
            status=self._status(outcome.state, agent_message),
            artifacts=artifacts,
            history=(dict(message), agent_message),
        )

    async def stream(
        self, message: Mapping[str, Any]
    ) -> AsyncIterator[_TaskEvent | _ArtifactEvent | _StatusEvent]:
        """Handle SendStreamingMessage / message/stream.

        Lazy on purpose — the `working` task reaches the caller before the agent
        is called, which is the point of the streaming method. Safe to be lazy
        because `_outcome` turns every failure into a terminal state rather than
        an exception, so the stream always ends with a terminal status event.
        """
        task_id, context_id = self._ids(message)
        yield _TaskEvent(
            _Task(
                task_id=task_id,
                context_id=context_id,
                status=self._status(STATE_WORKING),
                history=(dict(message),),
            )
        )

        outcome = await self._outcome(message.get("parts") or [])
        if outcome.output is not None:
            yield _ArtifactEvent(
                task_id=task_id,
                context_id=context_id,
                artifact=self._artifact(outcome.parts),
            )
        yield _StatusEvent(
            task_id=task_id,
            context_id=context_id,
            status=self._status(
                outcome.state,
                self._message(
                    task_id=task_id, context_id=context_id, parts=outcome.parts
                ),
            ),
            final=True,
        )


# ----------------------------------------------------------------------
# JSON-RPC plumbing
# ----------------------------------------------------------------------
# Best-effort id recovery from a body that would not parse. JSON-RPC allows a
# null id on a parse error, but echoing the id the caller can see in its own
# request makes a correlated log far easier to follow, so we try first.
_ID_IN_BROKEN_JSON = re.compile(rb'"id"\s*:\s*(?:"([^"]*)"|(-?\d+))')


def _recover_id(raw: bytes) -> Any:
    match = _ID_IN_BROKEN_JSON.search(raw)
    if match is None:
        return None
    text, number = match.groups()
    return text.decode("utf-8", "replace") if text is not None else int(number)


def _result(request_id: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": dict(result)}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _error_response(request_id: Any, code: int, message: str) -> JSONResponse:
    # HTTP 200 on purpose: in JSON-RPC the transport succeeded and the error is
    # the payload. A 4xx here would make well-behaved clients retry or give up.
    # Error objects are dialect-independent, so no dialect is needed here.
    return JSONResponse(status_code=200, content=_error(request_id, code, message))


def _message_or_error(body: Mapping[str, Any], request_id: Any) -> Any:
    params = body.get("params")
    if not isinstance(params, Mapping):
        return _error_response(
            request_id, ERROR_INVALID_PARAMS, "params must be an object"
        )
    message = params.get("message")
    if not isinstance(message, Mapping):
        return _error_response(
            request_id, ERROR_INVALID_PARAMS, "params.message must be an object"
        )
    parts = message.get("parts")
    if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
        return _error_response(
            request_id, ERROR_INVALID_PARAMS, "params.message.parts must be an array"
        )
    return message


def _sse(payload: Mapping[str, Any]) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def base_url_for(request: Request) -> str:
    """Public base URL of this deployment, for the card's endpoint."""
    return str(request.base_url).rstrip("/")


def add_a2a_routes(
    app: FastAPI,
    get_agent: Callable[[], _AgentLike],
    *,
    new_id: IdFactory = _uuid_id,
    now: Clock = _utc_now,
) -> None:
    """Mount the agent card and the JSON-RPC endpoint on an existing app."""
    handler = A2AHandler(get_agent, new_id=new_id, now=now)

    @app.get(AGENT_CARD_PATH)
    def agent_card(request: Request, dialect: str | None = None) -> Any:
        # Base URL comes from the live request, so the card is right on
        # localhost, a staging revision and the production URL alike.
        # `?dialect=0.3` serves the pre-v1.0 shape for an older consumer.
        if dialect in (None, "", Dialect.V1.value, "1", "latest"):
            return build_agent_card_v1(base_url_for(request))
        if dialect == Dialect.V03.value:
            return build_agent_card_v03(base_url_for(request))
        return JSONResponse(
            status_code=400,
            content={"detail": f"unknown dialect {dialect!r}; expected 1.0 or 0.3"},
        )

    @app.post(A2A_PATH)
    async def a2a(request: Request) -> Response:
        raw = await request.body()
        try:
            body = json.loads(raw or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error_response(
                _recover_id(raw), ERROR_PARSE, "request body is not valid JSON"
            )
        if not isinstance(body, Mapping):
            return _error_response(
                None, ERROR_INVALID_REQUEST, "request must be a JSON object"
            )

        request_id = body.get("id")
        method = body.get("method")
        route = _METHODS.get(method if isinstance(method, str) else "")
        if route is None:
            return _error_response(
                request_id, ERROR_METHOD_NOT_FOUND, f"unsupported method {method!r}"
            )

        message = _message_or_error(body, request_id)
        if isinstance(message, JSONResponse):
            return message

        dialect = dialect_of(method if isinstance(method, str) else None, message)
        _, streaming = route

        if not streaming:
            task = await handler.send(message)
            return JSONResponse(
                _result(request_id, _render_send_result(task, dialect))
            )

        async def event_stream() -> AsyncIterator[bytes]:
            # One yield per event: Starlette writes each chunk as it is produced,
            # so the caller sees `working` before the verdict exists.
            async for event in handler.stream(message):
                yield _sse(_result(request_id, _render_event(event, dialect)))

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Print an agent card — the JSON to paste into the console.

        uv run python -m savings_flow.agents.a01_id_verification.a2a <base-url>
        uv run python -m savings_flow.agents.a01_id_verification.a2a <base-url> 0.3
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if not 1 <= len(args) <= 2:
        sys.stderr.write(
            "usage: python -m savings_flow.agents.a01_id_verification.a2a"
            " <base-url> [1.0|0.3]\n"
        )
        return 2
    wanted = args[1] if len(args) == 2 else Dialect.V1.value
    try:
        dialect = Dialect(wanted)
    except ValueError:
        sys.stderr.write(f"unknown dialect {wanted!r}; expected 1.0 or 0.3\n")
        return 2
    # ensure_ascii=False: the card is pasted into a console by a human, and
    # \u2014 escapes make the description unreadable in the process.
    print(json.dumps(build_agent_card(args[0], dialect), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
