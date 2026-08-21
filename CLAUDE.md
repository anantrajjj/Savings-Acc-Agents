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

**A01 — ID Verification Agent: built.** Deterministic PAN + registry checks
with a single Gemini 3.7 Flash call adjudicating the name match only; mocked
PAN registry behind a Protocol; Cloud Run FastAPI mirroring the Agent Engine
`:query` envelope; 247 tests passing without credentials or network. See
`docs/agents/a01-id-verification.md`.

**Deployed and verified live** on Cloud Run in `asia-south1`
(`a01-id-verification`, runtime SA `a01-id-verification@sandboxa1`), calling
`gemini-3.7-flash` on the global endpoint through ADC. `POST /query` returns
the contract shape via the model path; `GET /status` is the externally
reachable health endpoint.

**Registered in Gemini Enterprise** as a custom A2A agent (`ENABLED`). The
service serves three surfaces: `/query` (the platform `:query` envelope),
`/a2a` (JSON-RPC, A2A v1.0 and v0.3 dialects on one endpoint), and
`/.well-known/agent-card.json`. Note the registration API only accepts the
**v0.3** card shape today, so the card URL carries `?dialect=0.3`; the runtime
answers both. The platform stores a card snapshot, so card changes need
`agents-cli publish` re-run. Untested: a real routed query from the assistant.

Remaining agents (A02, A03, A04, A20, A05, A24) are not started; each awaits
explicit go-ahead. **Read `docs/learnings.md` before starting one** — it records
the errors, GCP setup order, model behaviour, Cloud Run and A2A findings from
A01, and ends with a checklist.

### Decisions carried forward to the other agents

- **Model**: `gemini-3.7-flash` on the `global` endpoint. It has no regional
  data residency; `gemini-3.5-flash` in `asia-south1` is the in-region
  alternative. Chosen deliberately — revisit per agent if an agent handles more
  sensitive payloads than PAN plus name.
- **Contract discipline**: the LLM never authors the response envelope. It
  returns a narrow structured judgement under a response schema; deterministic
  code composes every field the platform validates, and the entrypoint never
  raises.
- **Shared modules**: `savings_flow.common` holds the `:query` envelope models,
  PAN validation, and the policy-citation catalog (stable IDs, `ids_for_agent`
  mapping) that every agent's `explain` block cites from.
