"""The request/response envelope the Gemini Enterprise platform speaks.

Agent Engine passes the POST body's `input` map as kwargs to `class_method`, so
a single-kwarg agent sees `{"input": {"input": {...}}}`. Cloud Run deployments
mirror that shape exactly, keeping caller code identical across both targets.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    class_method: str = "query"
    input: dict[str, Any] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: dict[str, Any]
