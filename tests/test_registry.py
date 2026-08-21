"""Tests for the mocked PAN registry."""

from __future__ import annotations

import pytest

from savings_flow.agents.a01_id_verification.registry import (
    DEFAULT_RECORDS,
    SOURCE,
    MockPanRegistry,
    PanRecord,
)
from savings_flow.common.pan import HOLDER_TYPES, check_pan


@pytest.fixture
def registry() -> MockPanRegistry:
    return MockPanRegistry()


def test_known_pan_hits(registry: MockPanRegistry) -> None:
    record = registry.lookup("ZZAPD1001A")
    assert record is not None
    assert record.registered_name == "Anjali Deshpande"
    assert record.holder_type == "Individual"
    assert record.status == "ACTIVE"


def test_unknown_well_formed_pan_returns_none(registry: MockPanRegistry) -> None:
    # Structurally valid, so the miss must come from the fixture set and not
    # from validation — the registry never invents a name for an unknown PAN.
    assert check_pan("ABCPE1234F").valid
    assert registry.lookup("ABCPE1234F") is None


@pytest.mark.parametrize(
    "garbage",
    ["", "   ", None, "ZZAPD", "ZZAPD1001A9999", "1234567890", "!!!", "-", "ZZAPD100$A"],
)
def test_garbage_input_returns_none(registry: MockPanRegistry, garbage: str | None) -> None:
    assert registry.lookup(garbage) is None


@pytest.mark.parametrize(
    "raw",
    ["zzapd1001a", " ZZAPD1001A ", "ZZAPD-1001-A", "zz apd 1001 a", "ZzApD1001a"],
)
def test_normalized_input_hits(registry: MockPanRegistry, raw: str) -> None:
    record = registry.lookup(raw)
    assert record is not None
    assert record.pan == "ZZAPD1001A"


def test_injected_records_replace_defaults() -> None:
    injected = PanRecord("AAAPA1111A", "Test Holder", "Individual", "ACTIVE")
    registry = MockPanRegistry({injected.pan: injected})

    assert registry.lookup("AAAPA1111A") is injected
    assert registry.lookup("ZZAPD1001A") is None


def test_injected_records_are_copied() -> None:
    records = {"AAAPA1111A": PanRecord("AAAPA1111A", "Test Holder", "Individual", "ACTIVE")}
    registry = MockPanRegistry(records)
    records.clear()

    assert registry.lookup("AAAPA1111A") is not None


def test_defaults_are_not_shared_between_instances() -> None:
    # White-box: prove __init__ copies rather than aliasing DEFAULT_RECORDS.
    MockPanRegistry()._records.clear()

    assert MockPanRegistry().lookup("ZZAPD1001A") is not None


def test_evidence_ref_format(registry: MockPanRegistry) -> None:
    assert registry.evidence_ref("abcpe-1234-f") == f"{SOURCE}:ABCPE1234F"
    assert SOURCE == "mock_pan_registry"


def test_evidence_ref_is_emitted_for_misses(registry: MockPanRegistry) -> None:
    # A miss is itself evidence, so the reference must not depend on a hit.
    assert registry.lookup("ABCPE1234F") is None
    assert registry.evidence_ref("ABCPE1234F") == "mock_pan_registry:ABCPE1234F"


@pytest.mark.parametrize("pan", sorted(DEFAULT_RECORDS))
def test_default_fixtures_are_structurally_valid(pan: str) -> None:
    record = DEFAULT_RECORDS[pan]
    check = check_pan(pan)

    assert check.valid, check.reason
    assert record.pan == pan, "dict key must match the record's own PAN"
    # Position 4 encodes the holder type; the fixture must not contradict it.
    assert record.holder_type == HOLDER_TYPES[pan[3]]
    assert record.holder_type == check.holder_type


@pytest.mark.parametrize("pan", sorted(DEFAULT_RECORDS))
def test_default_fixture_surname_initial_matches_position_five(pan: str) -> None:
    # Real PANs derive position 5 from the surname (or entity name) initial;
    # the corpus is only realistic if the fixtures do the same.
    record = DEFAULT_RECORDS[pan]
    words = record.registered_name.replace(".", " ").split()
    initials = {word[0].upper() for word in words}

    assert pan[4] in initials, f"{pan} position 5 not an initial of {record.registered_name!r}"


def test_fixture_statuses_cover_all_three_states() -> None:
    statuses = {record.status for record in DEFAULT_RECORDS.values()}

    assert statuses == {"ACTIVE", "INACTIVE", "DEACTIVATED"}


def test_fixtures_include_a_non_individual_holder() -> None:
    holder_types = {record.holder_type for record in DEFAULT_RECORDS.values()}

    assert holder_types - {"Individual"}
