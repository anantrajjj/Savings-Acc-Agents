"""Shared contract-checking helpers for the A01 test suites.

The Gemini Enterprise platform re-validates every agent response against its
published schema and fails the run on any mismatch, so the checks here are
deliberately stricter than Python's duck typing: they assert the *JSON* types
the platform will see, not merely values that behave correctly in Python.

Two ways to use the checker, both supported:

    def test_x(assert_matches_contract):       # fixture (preferred in tests)
        assert_matches_contract(payload)

    from conftest import assert_matches_contract   # plain import
    assert_matches_contract(payload)

Nothing here imports agent, service, registry, matching or adjudicator code —
this module must stay usable while those are still being written.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import pytest

# --- The published OUTPUT contract, as data ---------------------------------
# The schema fixture below is generated from these tuples so the human-readable
# schema and the executable checker can never drift apart.

REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = ("verified", "nameMatchScore", "explain")
OPTIONAL_TOP_LEVEL_KEYS: tuple[str, ...] = ("registeredName",)
REQUIRED_EXPLAIN_KEYS: tuple[str, ...] = (
    "reasoning_summary",
    "evidence_refs",
    "policy_citations",
    "confidence",
    "deciding_factors",
)
# The `explain` sub-keys that must be arrays of strings.
EXPLAIN_LIST_KEYS: tuple[str, ...] = (
    "evidence_refs",
    "policy_citations",
    "deciding_factors",
)

ContractAssertion = Callable[[Mapping[str, Any]], None]


# --- Primitive checks ------------------------------------------------------
# Failure messages name the offending key and what was wrong, because these
# strings are the whole diagnostic a future debugging session gets.


def _type_name(value: object) -> str:
    return type(value).__name__


def _assert_json_bool(label: str, value: object) -> None:
    # `isinstance(1, bool)` is False but `isinstance(True, int)` is True, so a
    # plain isinstance(bool) check is exactly the strictness we want here: it
    # rejects the truthy int / "true" string that JSON would carry through as a
    # non-boolean and blow up platform validation.
    assert isinstance(value, bool), (
        f"{label}: expected JSON boolean, got {_type_name(value)} ({value!r})"
    )


def _assert_unit_number(label: str, value: object) -> None:
    assert not isinstance(value, bool), (
        f"{label}: expected JSON number, got bool ({value!r}); "
        "booleans are not numbers on the wire"
    )
    assert isinstance(value, (int, float)), (
        f"{label}: expected JSON number, got {_type_name(value)} ({value!r})"
    )
    # NaN/inf fail this comparison too, which is what we want: they are not
    # representable in strict JSON.
    assert 0.0 <= float(value) <= 1.0, (
        f"{label}: expected a number within 0.0-1.0, got {value!r}"
    )


def _assert_non_empty_str(label: str, value: object) -> None:
    assert isinstance(value, str), (
        f"{label}: expected JSON string, got {_type_name(value)} ({value!r})"
    )
    assert value.strip(), f"{label}: expected a non-empty string, got {value!r}"


def _assert_str_list(label: str, value: object) -> None:
    assert isinstance(value, list), (
        f"{label}: expected JSON array, got {_type_name(value)} ({value!r})"
    )
    for index, item in enumerate(value):
        assert isinstance(item, str), (
            f"{label}[{index}]: expected JSON string, "
            f"got {_type_name(item)} ({item!r})"
        )


def _assert_key_set(
    label: str,
    payload: Mapping[str, Any],
    required: tuple[str, ...],
    optional: tuple[str, ...] = (),
) -> None:
    missing = [key for key in required if key not in payload]
    assert not missing, (
        f"{label}: missing required key(s) {sorted(missing)}; "
        f"contract requires {list(required)}"
    )
    allowed = set(required) | set(optional)
    unexpected = sorted(set(payload) - allowed)
    assert not unexpected, (
        f"{label}: unexpected key(s) {unexpected}; "
        f"contract allows only {sorted(allowed)}"
    )


# --- Public helpers --------------------------------------------------------


def assert_matches_contract(payload: Mapping[str, Any]) -> None:
    """Assert `payload` matches the published A01 OUTPUT contract.

    Raises AssertionError naming the offending key on the first violation.
    """
    assert isinstance(payload, Mapping), (
        f"payload: expected JSON object, got {_type_name(payload)} ({payload!r})"
    )
    _assert_key_set(
        "payload", payload, REQUIRED_TOP_LEVEL_KEYS, OPTIONAL_TOP_LEVEL_KEYS
    )

    _assert_json_bool("verified", payload["verified"])
    _assert_unit_number("nameMatchScore", payload["nameMatchScore"])

    # Optional key: absent is fine, present-but-junk is not.
    if "registeredName" in payload:
        _assert_non_empty_str("registeredName", payload["registeredName"])

    explain = payload["explain"]
    assert isinstance(explain, Mapping), (
        f"explain: expected JSON object, got {_type_name(explain)} ({explain!r})"
    )
    # `explain` has no optional members — exactly the five keys, no more.
    _assert_key_set("explain", explain, REQUIRED_EXPLAIN_KEYS)

    _assert_non_empty_str("explain.reasoning_summary", explain["reasoning_summary"])
    for key in EXPLAIN_LIST_KEYS:
        _assert_str_list(f"explain.{key}", explain[key])
    _assert_unit_number("explain.confidence", explain["confidence"])


def assert_json_round_trip(payload: Mapping[str, Any]) -> None:
    """Assert `payload` survives json.dumps -> json.loads unchanged.

    The platform sees JSON, never Python objects, so anything that mutates or
    fails in transit (tuples becoming lists, Decimal, datetime, NaN) is a
    contract break even though it looks fine in-process.
    """
    try:
        encoded = json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"payload is not JSON-serialisable: {exc}") from exc
    decoded = json.loads(encoded)
    assert decoded == payload, (
        "payload changed across a JSON round trip: "
        f"sent {payload!r}, got back {decoded!r}"
    )


# --- Fixtures --------------------------------------------------------------
# `name=` keeps the public callables importable under their own names while
# still exposing them as fixtures.


@pytest.fixture(name="assert_matches_contract")
def _assert_matches_contract_fixture() -> ContractAssertion:
    return assert_matches_contract


@pytest.fixture(name="assert_json_round_trip")
def _assert_json_round_trip_fixture() -> ContractAssertion:
    return assert_json_round_trip


@pytest.fixture
def platform_output_schema() -> dict[str, Any]:
    """JSON-Schema-shaped description of the OUTPUT contract.

    Hand-written on purpose: it documents what the platform publishes, and no
    `jsonschema` dependency is pulled in to read it. `assert_matches_contract`
    is the executable form of the same rules; both are built from the key
    tuples above so they stay in step.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(REQUIRED_TOP_LEVEL_KEYS),
        "properties": {
            "verified": {"type": "boolean"},
            "nameMatchScore": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            # Optional: omitted entirely when the registry returns no name.
            "registeredName": {"type": "string", "minLength": 1},
            "explain": {
                "type": "object",
                "additionalProperties": False,
                "required": list(REQUIRED_EXPLAIN_KEYS),
                "properties": {
                    "reasoning_summary": {"type": "string", "minLength": 1},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                    "policy_citations": {"type": "array", "items": {"type": "string"}},
                    "confidence": {
                        "type": "number",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                    "deciding_factors": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    }


@pytest.fixture
def sample_output() -> dict[str, Any]:
    """Minimal valid OUTPUT payload — the smallest thing the platform accepts."""
    return {
        "verified": True,
        "nameMatchScore": 0.97,
        "explain": {
            "reasoning_summary": "PAN found in registry; name matched.",
            "evidence_refs": [],
            "policy_citations": [],
            "confidence": 0.9,
            "deciding_factors": [],
        },
    }
