# SAVINGS FLOW

BFSI Savings Account Opening workflow, built as a suite of independent ADK
(Agent Development Kit, Python) agents.

## Goal

Each agent below is built and deployed **independently** as its own ADK
agent targeting the **Gemini Enterprise Agent Platform (Agent Runtime)**,
deployed via **Cloud Run**. There is no cross-agent orchestration code in
this repo — the workflow composition across agents happens separately, on
the Gemini Enterprise app side.

## Agents

Each agent carries an `agent_id` (process-numbering slot) as metadata only —
IDs are not sequential/contiguous by design (room reserved for agents outside
this initial set) and must NOT appear in the agent's name.

| ID  | Agent Name                              |
|-----|------------------------------------------|
| A01 | ID Verification Agent                     |
| A02 | Document Intelligence Agent               |
| A03 | Face Match & Liveness Agent                |
| A04 | Address and Contact Verification Agent    |
| A20 | Sanctions and PEP Screening Agent         |
| A05 | KYC Risk Categorization Agent             |
| A24 | Communication Composer Agent              |

## Conventions

- **Tooling**: `uv` only — no `pip`. Dependencies via `uv add` /
  `uv add --dev`, environment via `uv venv` / `uv sync`, running code via
  `uv run`.
- **Tools are mocked for now.** No real integrations (KYC bureaus, AML
  vendors, core banking, comms providers) — stub/mock implementations behind
  each agent's tool interface, built for progressive replacement as real
  integrations are added.
- **Testing is required.** Every agent needs test coverage (pytest).
- **Deployment target**: Cloud Run, one service per agent, registered on the
  Gemini Enterprise Agent Platform (Agent Runtime).
- **No Superpowers skills.** Do NOT invoke `superpowers:*` skills in this
  repo (brainstorming, TDD, writing-plans, etc.). Work directly. The Google
  ADK / gcloud / Cloud Run skills are fine to use.

## Status

Environment scaffolded (`uv init`, `google-adk`, `pytest` +
`pytest-asyncio` as dev deps). No agent code written yet — agent
implementation is pending explicit go-ahead per agent/batch.
