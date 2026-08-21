"""HTTP surface for A01 — Identity Verification Agent.

On Vertex Agent Engine the platform calls
`POST .../reasoningEngines/NNNN:query` and gets `{"output": {...}}` back. This
service is the Cloud Run twin of that endpoint: same request envelope, same
response envelope, so the Gemini Enterprise registration and every caller stay
byte-identical whichever target the agent is deployed to.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any, Protocol, cast

from fastapi import FastAPI, HTTPException

from savings_flow.common.envelope import QueryRequest, QueryResponse

_LOGGER = logging.getLogger(__name__)

# The only `class_method` Agent Engine ever invokes on this agent. Rejecting
# anything else keeps a typo from being silently answered as a valid query.
CLASS_METHOD = "query"

DEFAULT_PORT = 8080


class _AgentLike(Protocol):
    """The slice of the agent this service depends on."""

    def set_up(self) -> None: ...

    def query(self, *, input: dict[str, Any]) -> dict[str, Any]: ...


def _build_agent() -> _AgentLike:
    """Construct the real agent.

    Imported here rather than at module scope so that an injected agent — and
    the credential-free `/healthz` probe — never pull in the model client.
    """
    from savings_flow.agents.a01_id_verification.agent import A01Agent

    return cast("_AgentLike", A01Agent())


class _AgentSlot:
    """Holds the agent and guarantees `set_up()` runs exactly once.

    Startup normally warms this via the lifespan hook, but requests resolve it
    too: `TestClient` used without its context manager skips lifespan, and a
    lazy first request must not serve traffic against an un-set-up agent. The
    lock exists because uvicorn dispatches sync endpoints onto a thread pool,
    so two cold requests can race here.
    """

    def __init__(
        self, agent: _AgentLike | None, factory: Callable[[], _AgentLike]
    ) -> None:
        self._agent = agent
        self._factory = factory
        self._ready = False
        self._lock = threading.Lock()

    def get(self) -> _AgentLike:
        if self._ready:
            return self._agent  # type: ignore[return-value]
        with self._lock:
            if not self._ready:
                agent = self._agent if self._agent is not None else self._factory()
                agent.set_up()
                self._agent = agent
                self._ready = True
        return cast("_AgentLike", self._agent)


def create_app(agent: object | None = None) -> FastAPI:
    """Build the ASGI app, optionally against an injected agent (tests)."""
    slot = _AgentSlot(cast("_AgentLike | None", agent), _build_agent)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Fail the container start — and therefore the Cloud Run revision —
        # rather than accept traffic against a misconfigured agent.
        slot.get()
        yield

    app = FastAPI(title="A01 Identity Verification Agent", lifespan=lifespan)

    @app.post("/query", response_model=QueryResponse)
    def query(body: QueryRequest) -> QueryResponse:
        if body.class_method != CLASS_METHOD:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unsupported class_method {body.class_method!r}; "
                    f"expected {CLASS_METHOD!r}"
                ),
            )
        # Outer `input` is the kwargs map, inner `input` is the agent's named
        # parameter. Absent, null and empty all collapse to `{}` so the agent
        # always receives a dict and can answer with its own validation error.
        payload = body.input.get("input") or {}
        try:
            output = slot.get().query(input=payload)
        except Exception:
            # The agent is contracted never to raise, so reaching here is a
            # bug, not a rejected application. Log the trace, return none of it.
            _LOGGER.exception("A01 agent raised while handling a query")
            raise HTTPException(
                status_code=500, detail="internal error handling query"
            ) from None
        return QueryResponse(output=output)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        # Cloud Run startup/liveness probe: no credentials, no model call, no
        # agent touched — it answers whether the process is serving, nothing more.
        return {"status": "ok"}

    @app.get("/status")
    def status() -> dict[str, str]:
        # Same answer as /healthz, reachable from outside. Cloud Run's front end
        # intercepts /healthz on the public URL and returns its own 404, so an
        # external caller cannot use it to check the service; probes reach the
        # container directly and are unaffected.
        return {"status": "ok"}

    return app


app = create_app()


if __name__ == "__main__":
    import os

    import uvicorn

    # Cloud Run injects PORT; the default matches the Dockerfile's EXPOSE.
    uvicorn.run(
        app,
        host="0.0.0.0",  # noqa: S104 - container must accept the platform's traffic
        port=int(os.environ.get("PORT", DEFAULT_PORT)),
    )
