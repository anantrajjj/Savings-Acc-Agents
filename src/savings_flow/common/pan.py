"""PAN (Permanent Account Number) structural validation.

A PAN is ten characters: five letters, four digits, one letter. The fourth
character encodes the holder type and the fifth is the first letter of the
holder's surname (individuals) or entity name. No checksum is publicly
documented, so structure and holder type are all that can be checked offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PAN_PATTERN = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")

HOLDER_TYPES = {
    "P": "Individual",
    "C": "Company",
    "H": "Hindu Undivided Family",
    "F": "Firm / Limited Liability Partnership",
    "A": "Association of Persons",
    "T": "Trust",
    "B": "Body of Individuals",
    "L": "Local Authority",
    "J": "Artificial Juridical Person",
    "G": "Government Agency",
}


@dataclass(frozen=True)
class PanCheck:
    """Result of validating a PAN's structure."""

    pan: str
    valid: bool
    holder_type: str | None = None
    reason: str | None = None


def normalize_pan(raw: str) -> str:
    """Upper-case and strip separators; callers pass user-typed input."""
    return re.sub(r"[\s-]", "", (raw or "")).upper()


def check_pan(raw: str) -> PanCheck:
    """Validate PAN structure and decode the holder-type character."""
    pan = normalize_pan(raw)
    if not pan:
        return PanCheck(pan=pan, valid=False, reason="PAN is empty")
    if len(pan) != 10:
        return PanCheck(
            pan=pan, valid=False, reason=f"PAN must be 10 characters, got {len(pan)}"
        )
    if not PAN_PATTERN.match(pan):
        return PanCheck(
            pan=pan,
            valid=False,
            reason="PAN must match five letters, four digits, one letter",
        )
    holder_code = pan[3]
    if holder_code not in HOLDER_TYPES:
        return PanCheck(
            pan=pan,
            valid=False,
            reason=f"unknown holder-type character {holder_code!r} in position 4",
        )
    return PanCheck(pan=pan, valid=True, holder_type=HOLDER_TYPES[holder_code])
