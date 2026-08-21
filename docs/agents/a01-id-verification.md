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
