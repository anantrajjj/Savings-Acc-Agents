"""Mocked PAN registry — stands in for the CBDT/NSDL verification API.

The real service answers "is this PAN live, and whose name is on it?"; nothing
more is available from it, so `PanRecord` is deliberately thin. Swapping in a
real integration means writing another `PanRegistry` implementation, not
changing callers.

The fixture set doubles as the name-matching corpus for A01, so the registered
names are intentionally messy: initials, honorifics, transliteration variants
and surname-first ordering are exactly what a PAN record holds in practice
while the application form holds something else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from savings_flow.common.pan import normalize_pan

SOURCE = "mock_pan_registry"

# A PAN can be live, dormant, or struck off (duplicates, deceased holders).
# Only ACTIVE should ever clear identity verification on its own.
STATUS_ACTIVE = "ACTIVE"
STATUS_INACTIVE = "INACTIVE"
STATUS_DEACTIVATED = "DEACTIVATED"


@dataclass(frozen=True)
class PanRecord:
    """One PAN as the registry holds it."""

    pan: str
    registered_name: str
    holder_type: str
    status: str


class PanRegistry(Protocol):
    """Interface a real PAN verification integration will implement."""

    def lookup(self, pan: str) -> PanRecord | None: ...


# Synthetic PANs: the ZZ prefix is not issued in practice, so none of these can
# collide with a real holder. Each still obeys the real structure — position 4
# is the holder-type character and position 5 the surname initial — because the
# name matcher and PAN validator are exercised against this same set.
DEFAULT_RECORDS: dict[str, PanRecord] = {
    # exact match: applicant types the name exactly as registered
    "ZZAPD1001A": PanRecord("ZZAPD1001A", "Anjali Deshpande", "Individual", STATUS_ACTIVE),
    # initials vs expanded given names: form says "R. K. Sharma"
    "ZZBPS1002B": PanRecord("ZZBPS1002B", "Rajesh Kumar Sharma", "Individual", STATUS_ACTIVE),
    # surname-first ordering: form says "Padmanabhan Iyer"
    "ZZCPI1003C": PanRecord("ZZCPI1003C", "Iyer Padmanabhan", "Individual", STATUS_ACTIVE),
    # transliteration: form says "Muhammad Ilias Qureshi"
    "ZZDPQ1004D": PanRecord("ZZDPQ1004D", "Mohammed Ilyas Qureshi", "Individual", STATUS_ACTIVE),
    # transliteration: form says "Laxmi Narayanan"
    "ZZEPN1005E": PanRecord("ZZEPN1005E", "Lakshmi Narayanan", "Individual", STATUS_ACTIVE),
    # transliteration plus internal capital: form says "Hari KrishnaN"
    "ZZFPK1006F": PanRecord("ZZFPK1006F", "Hari Krishnan", "Individual", STATUS_ACTIVE),
    # honorific on the registry side: form says "Vivek Ganesan"
    "ZZGPG1007G": PanRecord("ZZGPG1007G", "Dr. Vivek Ganesan", "Individual", STATUS_ACTIVE),
    # honorific on the registry side: form says "Kavita Menon"
    "ZZHPM1008H": PanRecord("ZZHPM1008H", "Smt. Kavita Menon", "Individual", STATUS_ACTIVE),
    # married surname change: PAN still carries the maiden surname
    "ZZJPB1009J": PanRecord("ZZJPB1009J", "Priya Bhatnagar", "Individual", STATUS_ACTIVE),
    # South Indian expanded initial: form says "S. Venkatesan"
    "ZZKPV1010K": PanRecord("ZZKPV1010K", "Sundaram Venkatesan", "Individual", STATUS_ACTIVE),
    # negative case: same surname as ZZAPD1001A, genuinely a different person
    "ZZLPD1011L": PanRecord("ZZLPD1011L", "Mahesh Deshpande", "Individual", STATUS_ACTIVE),
    # dormant PAN: name may match but the record cannot clear verification
    "ZZMPT1012M": PanRecord("ZZMPT1012M", "Farhan Tabrez", "Individual", STATUS_INACTIVE),
    # struck off, e.g. surrendered as a duplicate allotment
    "ZZNPS1013N": PanRecord("ZZNPS1013N", "Ritu Sabharwal", "Individual", STATUS_DEACTIVATED),
    # non-individual holder: entity name, no surname to match against
    "ZZPCA1014P": PanRecord(
        "ZZPCA1014P", "Arcadia Textiles Private Limited", "Company", STATUS_ACTIVE
    ),
    # non-individual holder: HUF, registered in the karta's family name
    "ZZQHG1015Q": PanRecord(
        "ZZQHG1015Q", "Gopalakrishnan HUF", "Hindu Undivided Family", STATUS_ACTIVE
    ),
}


class MockPanRegistry:
    """In-memory `PanRegistry` for development and tests."""

    def __init__(self, records: dict[str, PanRecord] | None = None) -> None:
        # Copy so a caller mutating the default fixtures cannot leak across tests.
        self._records = dict(DEFAULT_RECORDS if records is None else records)

    def lookup(self, pan: str) -> PanRecord | None:
        """Return the record for `pan`, or None when it is not on file.

        An unknown-but-well-formed PAN is indistinguishable from a typo here, and
        inventing a name for it would silently manufacture identity evidence.
        """
        return self._records.get(normalize_pan(pan))

    def evidence_ref(self, pan: str) -> str:
        """Citable reference for the compliance trail, hit or miss."""
        return f"{SOURCE}:{normalize_pan(pan)}"
