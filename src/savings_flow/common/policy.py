"""Shared regulatory-citation catalog for every agent's `explain` block.

Every agent in the suite must populate `explain.policy_citations`. If each agent
invented its own strings, the same obligation would surface under a dozen
wordings and downstream compliance tooling could not aggregate them. So the
catalog lives here, keyed by stable IDs that agents reference but never rewrite.

**Citation string format** (stable — compliance tooling parses it)::

    "<id> — <source>, <clause>: <summary>"

for example::

    "KYC-CIP-001 — RBI Master Direction - Know Your Customer (KYC) Direction,
     2016, Section 16 (Customer Identification Procedure): The bank must ..."

The separator between id and source is an em dash surrounded by single spaces;
source and clause are comma-separated; the summary follows a colon and a space.
IDs match `ID_PATTERN` — ``DOMAIN-TOPIC-NNN``, all upper case.

**Locator accuracy — read before relying on this file.** The `source` values
are the real instruments. The `clause` values are *locators pending compliance
sign-off*: where the precise numbered provision was not certain, the clause
deliberately names the chapter or provision in words rather than guessing a
section number, so that a citation is never more precise than it is defensible.
Clauses carrying a number (``Section 12``, ``Rule 114B``) are the ones asserted
with confidence; clauses that are a bare descriptive phrase are the hedged ones
and must be pinned down by a reviewer. Nothing here is legal advice.

Adding entries: only add a policy some agent actually cites. An unused entry is
one more thing a bank reviewer has to validate for no benefit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ID_PATTERN = re.compile(r"^[A-Z]{3,6}-[A-Z]{3,10}-[0-9]{3}$")

# Full instrument titles, spelled once so every citation reads identically.
_RBI_KYC = "RBI Master Direction - Know Your Customer (KYC) Direction, 2016"
_PMLA = "Prevention of Money-Laundering Act, 2002"
_PMLR = "Prevention of Money-Laundering (Maintenance of Records) Rules, 2005"
_IT = "Income-tax Act, 1961 read with the Income-tax Rules, 1962"
_DPDP = "Digital Personal Data Protection Act, 2023"


@dataclass(frozen=True)
class Policy:
    """One citable regulatory obligation."""

    id: str
    source: str
    clause: str
    summary: str


_ENTRIES: tuple[Policy, ...] = (
    Policy(
        id="KYC-CIP-001",
        source=_RBI_KYC,
        clause="Section 16 (Customer Identification Procedure)",
        summary=(
            "The bank must identify the customer and obtain the Permanent Account "
            "Number, or Form 60 where the customer holds no PAN, before opening a "
            "deposit account."
        ),
    ),
    Policy(
        id="KYC-OVD-001",
        source=_RBI_KYC,
        clause="Section 3 (Definitions - officially valid document)",
        summary=(
            "Proof of identity and address must rest on a document from the defined "
            "list of officially valid documents; PAN is a mandatory identity "
            "credential collected alongside an OVD, not a substitute for one."
        ),
    ),
    Policy(
        id="KYC-VERIFY-001",
        source=_RBI_KYC,
        clause="Chapter VI (Customer Due Diligence)",
        summary=(
            "Identity details declared by the customer must be verified against the "
            "records of the issuing authority or another authorised verification "
            "source before the account is made operative."
        ),
    ),
    Policy(
        id="KYC-MISMATCH-001",
        source=_RBI_KYC,
        clause="Chapter VI (Customer Due Diligence)",
        summary=(
            "Where authoritative records differ from what the customer declared, the "
            "discrepancy must be resolved before customer due diligence is treated "
            "as complete; the customer's declaration alone is not sufficient."
        ),
    ),
    Policy(
        id="KYC-EDD-001",
        source=_RBI_KYC,
        clause="Enhanced and Simplified Due Diligence measures",
        summary=(
            "An unresolved identity discrepancy or any higher-risk indicator "
            "requires enhanced due diligence and approval by designated senior "
            "management before the relationship is established."
        ),
    ),
    Policy(
        id="KYC-DOC-001",
        source=_RBI_KYC,
        clause="Chapter VI (Customer Due Diligence)",
        summary=(
            "Documents relied upon for KYC must be checked against the original or "
            "the equivalent e-document for authenticity, validity and completeness "
            "before they are accepted as evidence."
        ),
    ),
    Policy(
        id="KYC-VCIP-001",
        source=_RBI_KYC,
        clause="Video-based Customer Identification Process (V-CIP)",
        summary=(
            "Where identity is established remotely, the bank must confirm the "
            "liveness of the person presented and match their live image against "
            "the photograph on the officially valid document."
        ),
    ),
    Policy(
        id="KYC-ADDRESS-001",
        source=_RBI_KYC,
        clause="Chapter VI (Customer Due Diligence) - proof of address",
        summary=(
            "The customer's address must be evidenced by an officially valid "
            "document; where that document does not carry the current address, a "
            "self-declared current address may be accepted on the prescribed terms."
        ),
    ),
    Policy(
        id="KYC-SANCTIONS-001",
        source=_RBI_KYC,
        clause="Obligations under international agreements - designated lists",
        summary=(
            "Before onboarding, the applicant must be screened against the United "
            "Nations Security Council designated lists, and no account may be "
            "opened in the name of a listed individual or entity."
        ),
    ),
    Policy(
        id="KYC-PEP-001",
        source=_RBI_KYC,
        clause="Accounts of Politically Exposed Persons (PEPs)",
        summary=(
            "A relationship with a politically exposed person, or their family or "
            "close associate, requires senior-management approval, establishment of "
            "the source of funds, and enhanced ongoing monitoring."
        ),
    ),
    Policy(
        id="KYC-RISK-001",
        source=_RBI_KYC,
        clause="Risk management and customer risk categorisation",
        summary=(
            "Every customer must be assigned a documented risk category on a "
            "risk-based assessment, and that category must drive the depth of due "
            "diligence and the intensity of ongoing monitoring."
        ),
    ),
    Policy(
        id="PMLA-REC-001",
        source=_PMLA,
        clause="Section 12",
        summary=(
            "The bank must maintain records of the identity evidence relied upon and "
            "of transactions, and preserve them for five years after the business "
            "relationship ends."
        ),
    ),
    Policy(
        id="PMLR-CDD-001",
        source=_PMLR,
        clause="Rule 9 (Client Due Diligence)",
        summary=(
            "Client identity must be verified at the commencement of an "
            "account-based relationship, and the identity information held must be "
            "kept current thereafter."
        ),
    ),
    Policy(
        id="PAN-STAT-001",
        source=_IT,
        clause="Section 139A read with Rule 114B",
        summary=(
            "PAN is allotted by the Income-tax Department and must be quoted when "
            "opening a bank account, which makes that Department's records the "
            "authoritative source for a PAN and its registered holder name."
        ),
    ),
    Policy(
        id="DPDP-NOTICE-001",
        source=_DPDP,
        clause="Section 5 (Notice)",
        summary=(
            "The customer must be given an itemised notice of the personal data "
            "being processed, the purposes of processing, and how to exercise their "
            "rights, at or before the point consent is sought."
        ),
    ),
)

# Treat as read-only: the type stays `dict` because callers annotate against it,
# but nothing in the suite may mutate the shared catalog.
CATALOG: dict[str, Policy] = {policy.id: policy for policy in _ENTRIES}

# Which obligations each agent is expected to cite. Agents pull from here rather
# than hard-coding ids, so a policy re-scoping is a one-file change.
_AGENT_POLICIES: dict[str, tuple[str, ...]] = {
    "A01": (
        "KYC-CIP-001",
        "KYC-OVD-001",
        "KYC-VERIFY-001",
        "KYC-MISMATCH-001",
        "KYC-EDD-001",
        "PAN-STAT-001",
        "PMLR-CDD-001",
        "PMLA-REC-001",
        "DPDP-NOTICE-001",
    ),
    "A02": (
        "KYC-OVD-001",
        "KYC-DOC-001",
        "KYC-VERIFY-001",
        "KYC-MISMATCH-001",
        "PMLA-REC-001",
        "DPDP-NOTICE-001",
    ),
    "A03": (
        "KYC-VCIP-001",
        "KYC-DOC-001",
        "PMLA-REC-001",
        "DPDP-NOTICE-001",
    ),
    "A04": (
        "KYC-ADDRESS-001",
        "KYC-OVD-001",
        "KYC-MISMATCH-001",
        "PMLR-CDD-001",
        "PMLA-REC-001",
        "DPDP-NOTICE-001",
    ),
    "A05": (
        "KYC-RISK-001",
        "KYC-EDD-001",
        "PMLR-CDD-001",
        "PMLA-REC-001",
    ),
    "A20": (
        "KYC-SANCTIONS-001",
        "KYC-PEP-001",
        "KYC-EDD-001",
        "PMLA-REC-001",
    ),
    "A24": (
        "DPDP-NOTICE-001",
        "PMLA-REC-001",
    ),
}


def get(policy_id: str) -> Policy:
    """Look up one policy by id, raising `KeyError` naming the unknown id."""
    try:
        return CATALOG[policy_id]
    except KeyError:
        raise KeyError(
            f"unknown policy id {policy_id!r}; "
            f"known ids: {', '.join(sorted(CATALOG))}"
        ) from None


def cite(*policy_ids: str) -> list[str]:
    """Format ids as citation strings for `explain.policy_citations`.

    Order is preserved so an agent can lead with its most decisive obligation.
    Unknown ids raise rather than degrade: a malformed citation reaching a
    compliance report is worse than a loud failure during the run.
    """
    return [
        f"{p.id} — {p.source}, {p.clause}: {p.summary}"
        for p in (get(policy_id) for policy_id in policy_ids)
    ]


def ids_for_agent(agent_id: str) -> list[str]:
    """Policy ids mapped to an agent, e.g. ``"A01"``.

    Raises `KeyError` for an unmapped agent: an agent emitting zero citations is
    a compliance gap, so a missing mapping must fail loudly, not return empty.
    """
    key = agent_id.strip().upper()
    try:
        return list(_AGENT_POLICIES[key])
    except KeyError:
        raise KeyError(
            f"no policies mapped for agent {agent_id!r}; "
            f"mapped agents: {', '.join(sorted(_AGENT_POLICIES))}"
        ) from None
