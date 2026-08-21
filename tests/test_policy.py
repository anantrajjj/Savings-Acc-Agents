"""Tests for the shared policy catalog.

The citation-format test is the load-bearing one: downstream compliance tooling
parses these strings, so the format is frozen here on purpose.
"""

from __future__ import annotations

import dataclasses

import pytest

from savings_flow.common import policy


def test_get_returns_the_matching_entry() -> None:
    entry = policy.get("KYC-CIP-001")
    assert entry is policy.CATALOG["KYC-CIP-001"]
    assert entry.id == "KYC-CIP-001"
    assert entry.source.startswith("RBI Master Direction")
    assert "Section 16" in entry.clause


def test_get_unknown_id_raises_keyerror_naming_the_id() -> None:
    with pytest.raises(KeyError) as excinfo:
        policy.get("KYC-NOPE-999")
    assert "KYC-NOPE-999" in str(excinfo.value)


def test_policy_is_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.get("KYC-CIP-001").summary = "tampered"  # type: ignore[misc]


def test_cite_matches_the_documented_format() -> None:
    entry = policy.get("PMLA-REC-001")
    (citation,) = policy.cite("PMLA-REC-001")
    assert citation == f"{entry.id} — {entry.source}, {entry.clause}: {entry.summary}"


def test_cite_returns_one_string_per_id_in_order() -> None:
    ids = ["KYC-EDD-001", "KYC-CIP-001", "PAN-STAT-001"]
    citations = policy.cite(*ids)
    assert len(citations) == len(ids)
    assert [c.split(" — ", 1)[0] for c in citations] == ids


def test_cite_with_no_ids_returns_empty() -> None:
    assert policy.cite() == []


def test_cite_unknown_id_raises_rather_than_emitting_a_broken_citation() -> None:
    with pytest.raises(KeyError) as excinfo:
        policy.cite("KYC-CIP-001", "MADE-UPXX-001")
    assert "MADE-UPXX-001" in str(excinfo.value)


def test_every_citation_in_the_catalog_is_formattable() -> None:
    citations = policy.cite(*policy.CATALOG)
    assert len(citations) == len(policy.CATALOG)
    for citation in citations:
        prefix, remainder = citation.split(" — ", 1)
        assert prefix in policy.CATALOG
        assert ": " in remainder


def test_ids_for_agent_a01_is_non_empty_and_fully_resolvable() -> None:
    ids = policy.ids_for_agent("A01")
    assert ids
    assert all(policy_id in policy.CATALOG for policy_id in ids)


@pytest.mark.parametrize(
    "agent_id", ["A01", "A02", "A03", "A04", "A05", "A20", "A24"]
)
def test_every_mapped_agent_resolves_to_known_unique_ids(agent_id: str) -> None:
    ids = policy.ids_for_agent(agent_id)
    assert ids, f"{agent_id} has no policies mapped"
    assert len(set(ids)) == len(ids), f"{agent_id} maps a duplicate policy id"
    assert all(policy_id in policy.CATALOG for policy_id in ids)


def test_ids_for_agent_is_case_insensitive_and_a_copy() -> None:
    ids = policy.ids_for_agent("a01")
    assert ids == policy.ids_for_agent("A01")
    ids.clear()
    assert policy.ids_for_agent("A01"), "mutating the result must not drain the map"


def test_ids_for_agent_unknown_agent_raises() -> None:
    with pytest.raises(KeyError) as excinfo:
        policy.ids_for_agent("A99")
    assert "A99" in str(excinfo.value)


def test_every_entry_has_all_four_fields_populated() -> None:
    for policy_id, entry in policy.CATALOG.items():
        for field in ("id", "source", "clause", "summary"):
            value = getattr(entry, field)
            assert isinstance(value, str)
            assert value.strip(), f"{policy_id}.{field} is empty"


def test_ids_are_unique_and_match_the_documented_pattern() -> None:
    ids = [entry.id for entry in policy._ENTRIES]
    assert len(set(ids)) == len(ids), "duplicate policy id in the catalog"
    assert sorted(ids) == sorted(policy.CATALOG)
    for policy_id in ids:
        assert policy.ID_PATTERN.match(policy_id), f"{policy_id} breaks ID_PATTERN"


def test_catalog_key_matches_entry_id() -> None:
    assert all(key == entry.id for key, entry in policy.CATALOG.items())


def test_catalog_covers_what_a01_needs() -> None:
    # A01 must be able to cite each of these obligations by id, so a future
    # re-scoping cannot silently strip one of them from its citation list.
    required = {
        "KYC-CIP-001",  # PAN collected during customer identification
        "KYC-VERIFY-001",  # verified against the issuing authority's records
        "KYC-MISMATCH-001",  # name-mismatch handling
        "KYC-EDD-001",  # escalation on mismatch
        "PMLA-REC-001",  # record-keeping of the evidence relied upon
    }
    assert required <= set(policy.ids_for_agent("A01"))
