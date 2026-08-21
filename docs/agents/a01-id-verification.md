# A01 — ID Verification Agent

Verifies an applicant's PAN and that the name they submitted matches the name
held against that PAN. Deployed independently to Cloud Run; composed into the
Savings Account Opening workflow on the Gemini Enterprise side.

## Contract

The platform re-validates every response and fails the run on mismatch.

**Input** (the `input` kwarg)

| Field      | Type   | Notes                          |
|------------|--------|--------------------------------|
| `pan`      | string | Case/format tolerant on input  |
| `fullName` | string | As declared by the applicant   |

Unknown keys are ignored — the surrounding workflow will grow.

**Output**

| Field            | Type    | Notes                                     |
|------------------|---------|-------------------------------------------|
| `verified`       | boolean | True only when nothing blocks it          |
| `nameMatchScore` | number  | 0.0–1.0, three decimal places             |
| `registeredName` | string  | Optional; omitted when no record was found |
| `explain`        | json    | `reasoning_summary`, `evidence_refs[]`, `policy_citations[]`, `confidence`, `deciding_factors[]` |

`query` never raises and never returns a partial shape. Every failure —
malformed input, invalid PAN, unknown PAN, model outage — is expressed inside
the contract as `verified: false` with a populated `explain`.

## Decision flow

1. **Parse** input (`contract.A01Input`). Blank or missing fields → refusal.
2. **PAN structure** (`common/pan.py`): five letters, four digits, one letter;
   position 4 decodes the holder type. Invalid → refusal, no model call.
3. **Registry lookup** (`registry.py`, mocked): unknown PAN → refusal, no model
   call. A well-formed but unknown PAN never yields a fabricated name.
4. **Deterministic name match** (`matching.py`): normalise (honorifics stripped,
   diacritics folded), tokenise, score initial-vs-expanded compatibility and
   `rapidfuzz` similarity.
5. **Model adjudication** (`adjudicator.py`): one Gemini 3.7 Flash call with a
   response schema, judging the name pair only. Any failure falls back to the
   deterministic score and says so in `explain`.
6. **Compose** (`agent.py`): thresholds and blockers applied, `explain` built.

### Thresholds and blockers

- `nameMatchScore >= 0.85` → verified (`contract.VERIFIED_THRESHOLD`).
- `0.65–0.85` → not verified, but flagged `name_match_referral_band` rather
  than a flat rejection. The contract has no third state, so the nuance lives
  in `explain.deciding_factors`.
- Hard blockers the model cannot override: PAN record status other than
  `ACTIVE`, and a holder type other than `Individual` (a company or HUF PAN
  cannot establish a natural person's identity).

### Why the model does not author the response

The model receives only the two names and the deterministic baseline, and can
return only `{score, factors, reasoning}`. Deterministic code owns the verdict,
the envelope, and every field the platform validates. A model that upgrades the
baseline by more than 0.25 has its score clamped (downward revisions are always
honoured) — in KYC a false accept costs far more than a referral.

## Layout

```
src/savings_flow/
  common/{envelope,pan,policy}.py     # shared across the agent suite
  agents/a01_id_verification/
    contract.py     # the platform contract as Pydantic models
    registry.py     # mocked PAN registry behind a Protocol
    matching.py     # deterministic Indian-name baseline
    adjudicator.py  # Gemini 3.7 Flash structured-output call
    agent.py        # composition; A01Agent.set_up() / .query()
    service.py      # Cloud Run FastAPI mirroring the :query envelope
deploy/a01/         # Dockerfile + deploy.sh
tests/              # pytest; no credentials or network required
```

`A01Agent` is deployment-agnostic: the same `set_up()` / `query(*, input)` class
satisfies Vertex Agent Engine's protocol if that target is ever revisited.

## Envelope

Cloud Run mirrors the Agent Engine call shape exactly, so caller code and the
Gemini Enterprise registration are identical either way:

```
POST https://<service-url>/query
{"class_method": "query", "input": {"input": {"pan": "…", "fullName": "…"}}}
→ {"output": {…agent output…}}
```

`GET /healthz` serves Cloud Run's startup and liveness probes without
credentials or a model call. Cloud Run's front end intercepts `/healthz` on the
public URL and answers its own 404, so external callers and uptime checks
should use `GET /status`, which returns the same payload. Probes reach the
container directly and are unaffected.

## A2A surface

The same Cloud Run service also speaks [A2A](https://a2a-protocol.org), so the
agent can be registered in Gemini Enterprise as a **Custom agent via A2A**
instead of through the `/query` envelope. `/query`, `/healthz` and `/status` are
unchanged; A2A is additive.

| Route | Method | Purpose |
|-------|--------|---------|
| `/.well-known/agent-card.json` | GET | Agent card. v1.0 shape by default; `?dialect=0.3` returns the pre-v1.0 shape. |
| `/a2a` | POST | JSON-RPC 2.0 endpoint. |

### Two dialects, one implementation

A2A v1.0 renamed the operations, dropped the `kind` discriminator from `Part`,
moved enums to `SCREAMING_SNAKE_CASE`, and replaced the card's single `url` with
`supportedInterfaces[]`. Google's Agent Registry still speaks v0.3 alongside
v1.0, so both are served: the **method name decides the dialect**, and the reply
is rendered in that same dialect. Everything between the parser and the renderer
is dialect-neutral, so there is one code path for the actual work.

| | v1.0 (default) | v0.3 |
|---|---|---|
| Send | `SendMessage` | `message/send` |
| Stream | `SendStreamingMessage` | `message/stream` |
| Send result | `result.task` | bare Task with `kind: "task"` |
| Part | `{"text": …}` / `{"data": {…}}` | `{"kind": "text", …}` / `{"kind": "data", …}` |
| Role | `ROLE_USER` / `ROLE_AGENT` | `user` / `agent` |
| State | `TASK_STATE_COMPLETED` … | `completed`, `input-required`, `failed`, `working` |
| Stream events | `{"task": …}`, `{"artifactUpdate": …}`, `{"statusUpdate": …}` | `kind: "task"`, `"artifact-update"`, `"status-update"` + `final` |

Any other method (`GetTask`, `CancelTask`, `tasks/get`, …) answers JSON-RPC
`-32601`. Malformed JSON is `-32700`, bad `params` is `-32602`. All three are
**HTTP 200 with an `error` member** — that is how JSON-RPC reports errors — and
they echo the request `id` (best-effort even when the body would not parse).

### What it accepts

`params.message.parts` is read liberally, because a caller may be another agent
or a person typing a sentence:

1. **A data part** — any part carrying a `data` object, whatever its `kind`
   (v1.0 has no `kind` at all). `pan` plus `fullName`, `full_name` or `name`.
2. **A text part** — any part carrying `text`. The PAN comes from a
   case-insensitive `[A-Za-z]{5}[0-9]{4}[A-Za-z]` search; the name is whatever
   remains after removing the PAN token and a short list of label/filler words
   (`pan`, `name`, `verify`, `identity`, `for`, `is`, …). `Verify PAN ZZBPS1002B
   for R. K. Sharma` → `{"pan": "ZZBPS1002B", "fullName": "R. K. Sharma"}`.

   The name step is a filter, not a parser: trailing prose is kept
   (`… for R. K. Sharma who applied today` yields the whole tail as the name).
   A caller that needs precision should send a data part. The agent's own name
   matcher still owns the verdict, so a sloppy extraction degrades into a low
   score, never a false accept.

Data parts always win over text parts.

If the PAN or the name cannot be determined, **the agent is not called at all**.
The reply is a Task in state `input-required` (`TASK_STATE_INPUT_REQUIRED`) whose
message names exactly which field is missing — guessing would manufacture KYC
evidence.

### What it returns

A Task whose `status.message` and whose single artifact
(`name: "a01-identity-verification"`) each carry **two parts**:

- a text part: a one-line verdict plus `explain.reasoning_summary`, for the
  assistant to render;
- a data part: the **complete, unmodified** agent output dict — exactly what
  `POST /query` returns — for downstream agents to consume.

`contextId` is echoed when the caller supplies one and generated otherwise.
`history` holds the inbound message plus the agent message. An unexpected
exception inside the agent (which is contracted not to raise) becomes a Task in
state `failed` with no internals in the payload.

Streaming emits three events, one SSE `data:` frame each, flushed as produced:
the Task in `working` state (before the agent is called), then the artifact
update, then the terminal `completed` status — carrying `final: true` in v0.3,
and relying on the terminal state plus stream closure in v1.0, which removed
that field.

### Registering it

Gemini Enterprise takes the card as **inline JSON pasted into the console**;
hosting it at the well-known URL is good practice, not a requirement. Print it
with:

```sh
uv run python -m savings_flow.agents.a01_id_verification.a2a \
  https://a01-id-verification-zg3lpq3eda-el.a.run.app        # v1.0 card
uv run python -m savings_flow.agents.a01_id_verification.a2a \
  https://a01-id-verification-zg3lpq3eda-el.a.run.app 0.3    # v0.3 card
```

(`build_agent_card_v1` / `build_agent_card_v03` in `a2a.py` are the same thing
as functions.) The console documents the v0.3 card shape while Agent Registry
accepts the v1.0 `supportedInterfaces` form — try v1.0 first, fall back to v0.3
if the field rejects it. The card's `description` and skill `description` are
what the assistant routes on, so they name the input, the verdict and the audit
block explicitly, and say what the agent does *not* do.

Authentication is plain Cloud Run IAM; the card declares no security scheme.

### Caveat

These shapes follow the published A2A v1.0 and v0.3 specs and are covered by
`tests/test_a2a.py` in both dialects, but **no real Gemini Enterprise call has
exercised them yet**. Two details to re-check against a live call: the v0.3
stream-event `kind` literals (`status-update` / `artifact-update`, per the
official v0.3→v1.0 migration guide), and whether the console's card field
tolerates the v1.0 `supportedInterfaces` array.

## Configuration

| Env var                 | Default             | Notes                                    |
|-------------------------|---------------------|------------------------------------------|
| `A01_MODEL`             | `gemini-3.7-flash`  | Latest Flash, GA 13 Aug 2026             |
| `GOOGLE_CLOUD_PROJECT`  | `sandboxa1`         |                                          |
| `GOOGLE_CLOUD_LOCATION` | `global`            | 3.7 Flash is global-endpoint only        |
| `PORT`                  | `8080`              | Set by Cloud Run                         |

**Data residency:** the service runs in `asia-south1`, but `gemini-3.7-flash`
is served only from the global endpoint, so name-adjudication calls leave the
region. `gemini-3.5-flash` is the newest Flash with in-region residency — set
`A01_MODEL=gemini-3.5-flash` and `GOOGLE_CLOUD_LOCATION=asia-south1` to keep
inference in India at the cost of one model generation. This was an explicit
project decision, not an oversight.

## Running it

```sh
uv run pytest                    # full suite, no credentials needed
uv run uvicorn savings_flow.agents.a01_id_verification.service:app --port 8080
bash deploy/a01/deploy.sh        # Cloud Run, project sandboxa1, asia-south1
```

Without credentials the agent still answers: the adjudicator falls back to the
deterministic score and records `model_unavailable_deterministic_score_used` in
`explain.deciding_factors`. That is the degraded mode, not a failure mode.

## Known gaps

- **Tools are mocked.** `registry.py` is a fixture set standing in for a
  CBDT/NSDL PAN verification API, behind the `PanRegistry` Protocol so a real
  integration replaces it without touching the agent.
- **Policy locators need compliance sign-off.** `common/policy.py` hedges
  clause references it cannot state confidently; see the flags in that module's
  docstring.
- **Transliteration is the deterministic baseline's weak spot** — `Laxmi` vs
  `Lakshmi` scores ~0.53 offline. That gap is precisely what the model call is
  there to close, so a live smoke test against Gemini is worth running before
  anyone judges match quality.

## Registered with Gemini Enterprise

Registered as a **custom A2A agent** on the app
`gemini-enterprise-17872941_1787294139191`, agent id `18067469415698243285`,
state `ENABLED`:

```sh
agents-cli publish gemini-enterprise \
  --agent-card-url "https://<service-url>/.well-known/agent-card.json?dialect=0.3" \
  --gemini-enterprise-app-id projects/378068182070/locations/global/collections/default_collection/engines/gemini-enterprise-17872941_1787294139191 \
  --deployment-target cloud_run --display-name "ID Verification Agent" --project sandboxa1
```

Two findings from doing it, both measured rather than assumed:

- **The registration API requires the v0.3 card shape.** Posting the v1.0 card
  (endpoints under `supportedInterfaces`) is rejected with
  `400 INVALID_ARGUMENT: required property 'protocolVersion' not found in
  object`. Hence `?dialect=0.3` on the card URL. The runtime still answers both
  dialects, so a v1.0 caller works even though the registration is described in
  v0.3 terms.
- **The platform stores a snapshot of the card**, not a live reference — the
  registration holds `a2aAgentDefinition.jsonAgentCard`. Changing the served
  card therefore has no effect until `agents-cli publish` is re-run, which
  updates the existing registration in place rather than duplicating it.

Gemini Enterprise calls the service as the Discovery Engine service agent, which
needs `roles/run.servicesInvoker` on the Cloud Run service:
`service-<PROJECT_NUMBER>@gcp-sa-discoveryengine.iam.gserviceaccount.com`.
