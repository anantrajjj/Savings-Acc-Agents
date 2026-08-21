# Learnings from building A01

Everything below was hit, measured, or corrected while building the ID
Verification Agent (A01) and getting it live on Cloud Run and registered in
Gemini Enterprise. It is written for whoever builds A02–A24 next. Errors are
recorded with the symptom first, because that is how you will meet them again.

A one-page checklist distilled from all of this sits at the end.

---

## 1. Toolchain and environment

### gcloud will not run on macOS's system Python

**Symptom.** `install.sh` dies with
`TypeError: unsupported operand type(s) for |: 'type' and 'type'`, then every
invocation prints *"You are running gcloud with Python 3.9, which is no longer
supported"*.

**Cause.** macOS ships Python 3.9.6; gcloud requires 3.10–3.14. The bundled
interpreter that ships inside the SDK is not picked up automatically.

**Fix.** Point `CLOUDSDK_PYTHON` at a modern interpreter. The uv-managed one is
already on the machine:

```sh
export CLOUDSDK_PYTHON="$HOME/.local/share/uv/python/cpython-3.13-macos-aarch64-none/bin/python3.13"
```

Because the installer crashed before finishing, it never wrote the PATH lines
either — `path.zsh.inc` and `completion.zsh.inc` had to be sourced from
`~/.zshrc` by hand. Verify with a *fresh login shell* (`zsh -lic 'gcloud
version'`), not the shell that has your ad-hoc exports.

### Installing agents-cli

`uv tool install google-agents-cli` — never `pip`. Installs two executables
(`agents-cli`, `google-agents-cli`) into `~/.local/bin`. It logs
`Malformed skill file (missing metadata.version)` from its own bundled skills on
every run; harmless, filter it out of captured output.

### A shell quirk that will waste ten minutes

Inside compound commands (`for` loops, command substitution) the sandbox shell
sometimes lost `/usr/bin`, producing `command not found: curl` and
`command not found: head` for tools that plainly exist. Either export a full
PATH at the top of the command or call binaries absolutely (`/usr/bin/curl`).
Don't debug it as a networking problem.

---

## 2. GCP project setup

### APIs

Enabled for A01 (`aiplatform`, `discoveryengine` and `iamcredentials` were
already on in `sandboxa1`):

| API | Needed for |
|---|---|
| `aiplatform.googleapis.com` | Gemini calls |
| `run.googleapis.com` | the service |
| `cloudbuild.googleapis.com` | `--source` deploys |
| `artifactregistry.googleapis.com` | built images |
| `cloudresourcemanager.googleapis.com` | IAM bindings |
| `discoveryengine.googleapis.com` | Gemini Enterprise / Agent registration |

### Identities, and which needs what

Four distinct principals matter. Confusing them costs a debugging cycle each.

| Principal | Role | Why |
|---|---|---|
| `a01-id-verification@<project>.iam.gserviceaccount.com` | `roles/aiplatform.user` | the revision's runtime identity, calling Gemini |
| `<PROJECT_NUMBER>-compute@developer.gserviceaccount.com` | `roles/cloudbuild.builds.builder` | Cloud Build during `--source` deploys |
| `service-<PROJECT_NUMBER>@gcp-sa-discoveryengine.iam.gserviceaccount.com` | `roles/run.servicesInvoker` | **Gemini Enterprise calls your service as this** |
| a human or CI account | `roles/run.invoker` | smoke tests against a private service |

Give the revision its own service account (`--service-account`) rather than the
default compute account, which tends to carry `roles/editor`. Least privilege
here is one flag.

### Ordering trap

`gcloud run services add-iam-policy-binding` fails with
`NOT_FOUND: Resource 'a01-id-verification' of kind 'SERVICE' … does not exist`
if you run it before the first deploy. Service-scoped bindings come *after* the
service exists; project-scoped ones can come before.

### `gcloud auth login` ≠ ADC

They are separate consents. The first authorizes the CLI; libraries in your
code need **Application Default Credentials** (`gcloud auth
application-default login`). Symptom of forgetting: the agent silently
degrades to its fallback path while `gcloud` commands work fine.

Also: `roles/owner` already carries permission to invoke a private Cloud Run
service, so a smoke test can succeed while a *service* caller still gets 403.
Don't infer "the invoker binding works" from your own curl.

---

## 3. Model behaviour (gemini-3.7-flash)

### Data residency is a real decision, not a footnote

`gemini-3.7-flash` (GA 13 Aug 2026) is served **only from the global
endpoint** — no regional data residency. `gemini-3.5-flash` is the newest Flash
available in `asia-south1`. For BFSI work under Indian localisation
expectations that is a decision to put in front of a human, with the trade
stated plainly: latest model vs inference staying in-region. A01 runs in
`asia-south1` and calls `global`, by explicit choice.

Keep the model id and location in env vars (`A01_MODEL`,
`GOOGLE_CLOUD_LOCATION`) so the swap is configuration, not a rebuild.

### Thinking tokens make short prompts slow

A two-name comparison — 183 input tokens, 93 output — consumed **456 thinking
tokens**, ~730 total, 2.8–4.0s per call.

**Symptom of getting this wrong.** `google.genai.errors.ServerError: 504
DEADLINE_EXCEEDED` on roughly half of calls with an 8s HTTP timeout, each one
silently falling back to the deterministic score — i.e. degrading exactly the
capability the model was added to provide.

**Fix.** 20s timeout (Cloud Run's request timeout is 120s, so there is room).
Budget for thinking latency on *every* call, however small the prompt looks.

### Calibrate guardrails against measured numbers, then lock them in a test

A01 clamps how far the model may lift the deterministic name-match score. The
first value, 0.25, was reasonable on paper and wrong in practice: the model
correctly scored `Laxmi`/`Lakshmi` at 0.98 against a 0.53 baseline, the clamp
held it to 0.775, and it landed in the referral band — the guardrail defeated
the one case the model existed for. 0.35 clears a single transliterated token
while still refusing to lift a two-token mismatch (0.43 baseline) over the
line.

The lesson is the follow-up: that calibration is now asserted by a test that
calls the real matcher, so a future change to normalisation cannot silently
invalidate the margin. A number chosen by measurement deserves a test, not a
comment.

### Silence the AFC warning

`google-genai` logs *"Direct use of automatic function calling (AFC) … is not
recommended"* on every `generate_content` call even with no tools configured.
It will fill your Cloud Run logs.

---

## 4. Contract discipline

The platform re-validates every response and fails the run on mismatch. What
worked:

- **The entrypoint never raises.** Malformed input, invalid PAN, unknown PAN,
  model outage, internal bug — all return `verified: false` with a populated
  `explain`. There is no error field in the contract, so failure has to be
  expressible inside the success shape.
- **The LLM never authors the envelope.** It receives the two names plus the
  deterministic baseline and may return only `{score, factors, reasoning}`
  under a response schema. Deterministic code composes every field the platform
  validates. Structured output is necessary but not sufficient — a model free-
  typing the top-level JSON is how you fail platform-side validation.
- **Deterministic facts outrank the model.** PAN structure, registry status and
  holder type are hard blockers: a perfect name match on a `DEACTIVATED` PAN, or
  on a PAN issued to a company, still returns `verified: false`.
- **Test the guardrail itself.** The contract checker is fed deliberately broken
  payloads (17 of them: `verified` as `"true"`, `verified` as `1`,
  `nameMatchScore` as a bool, score `1.5`, `explain` missing a sub-key,
  `evidence_refs` containing an int, blank `reasoning_summary`, …) and must
  raise on each. A guardrail nobody tested is not a guardrail.
- **Keep the platform's casing.** `nameMatchScore`, `fullName`,
  `registeredName` stay camelCase in the Pydantic models. Renaming them to be
  Pythonic and mapping at the edge is one more place to get it wrong.

Two smaller things worth copying: `to_payload()` omits optional
`registeredName` rather than sending `null`, and rounds the score to 3 dp; and
`confidence` describes certainty *of the verdict* (lowest near the threshold),
which is defensible but reads oddly — a `verified: true` at 0.875 reports
confidence 0.65. Decide what that field means per agent and document it.

---

## 5. Cloud Run specifics

### `/healthz` is intercepted on the public URL

**Symptom.** `GET /healthz` returns a Google HTML 404 with no `server` header,
while the route demonstrably exists.

**Diagnosis that settled it** — worth reusing whenever you suspect a proxy:

| Request | Result | Conclusion |
|---|---|---|
| `GET /openapi.json` | 200, lists `/healthz` | the app has the route |
| `GET /nonexistent` | `{"detail":"Not Found"}`, `server: Google Frontend` | our app answers unknown paths |
| `GET /healthz` | HTML 404, no `server` header | never reached the container |

Startup probes on `/healthz` still pass, because probes hit the container
directly — so health checking is real, only external access is swallowed. Fix:
keep `/healthz` for probes, add `/status` for operators and uptime checks.

### `gcloud run deploy --source` has no Dockerfile-path flag

It builds with the Dockerfile at the *root* of the source directory, and the
build context must be the repo root (`pyproject.toml`, `uv.lock`, `src/`). With
one Dockerfile per agent, `deploy.sh` stages `deploy/<agent>/Dockerfile` to the
repo root for the duration of the deploy, removes it with `trap … EXIT`, and
refuses to start if a root `Dockerfile` already exists rather than clobbering
one.

### Anything self-advertising a URL must be checked on the deployed instance

**Symptom.** The A2A agent card served from Cloud Run advertised
`http://…/a2a`. Tests were green.

**Cause.** Cloud Run terminates TLS at its front end and forwards plain HTTP,
so `request.base_url` reports `http`. This is not cosmetic — it is the address
other agents are told to call, over a scheme Cloud Run does not serve.

**Fix.** Honour `X-Forwarded-Proto`, default to `https` for anything that is
not a local development host, and keep `http` for `localhost` so the card stays
usable on a laptop. Two tests now cover it. The general rule: a value that
describes *where this service lives* cannot be fully verified in-process.

### Two URLs, one service

`gcloud run deploy` prints a project-number URL
(`…-<PROJECT_NUMBER>.<region>.run.app`) while `services describe
--format='value(status.url)'` returns the hash-style one. Both route. Pick the
one from `describe` for anything you store, and be consistent.

---

## 6. A2A protocol

### Read the proto, not the prose

Documentation fetches were repeatedly truncated or contradictory, including on
field names. The normative source is
`specification/a2a.proto` in `github.com/a2aproject/A2A`. Several guessed raw
paths 404 — use the GitHub contents API to discover the real layout:

```
https://api.github.com/repos/a2aproject/A2A/contents/specification
```

Latest release at the time of writing: **v1.0.1** (28 May 2026). One search
summary claimed "A2A has reached 1.2 with signed agent cards"; the releases API
disagreed, and `AgentCard` does carry a `signatures` field, which is probably
what that claim garbled. Prefer the artifact over the summary.

### v1.0 broke v0.3

| | v0.3 | v1.0 |
|---|---|---|
| Methods | `message/send`, `message/stream` | `SendMessage`, `SendStreamingMessage` |
| `Part` | `"kind": "text"｜"data"｜"file"` | oneof `text` / `raw` / `url` / `data`, **no discriminator** |
| Role | `"user"` / `"agent"` | `ROLE_USER` / `ROLE_AGENT` |
| Task state | `"completed"`, `"input-required"` | `TASK_STATE_COMPLETED`, `TASK_STATE_INPUT_REQUIRED` |
| Endpoint in card | one `url` | `supportedInterfaces[]` of `{url, protocolBinding, protocolVersion}` |
| Stream events | `kind: "status-update"` / `"artifact-update"`, with `final` | `statusUpdate` / `artifactUpdate` oneof, no `final` |

Also from the proto: `SendMessageResponse` is `oneof payload { Task task;
Message message; }` — so a JSON-RPC success is `result.task`. `Artifact` carries
`artifact_id` → `artifactId`. Protocol bindings are **upper case**: the proto
says *"The core ones officially supported are `JSONRPC`, `GRPC` and
`HTTP+JSON`"*. Note that Google's Agent Registry `gcloud` surface spells its own
binding values lower case (`http-json`) — different surface, different
vocabulary; don't cross-apply.

### Google's surfaces disagree about which version they accept

**Symptom.** Registering the spec-correct v1.0 card:

```
400 INVALID_ARGUMENT: JSON agent card format is invalid:
required property 'protocolVersion' not found in object
```

The Gemini Enterprise **app registration** API requires the v0.3 card shape
(top-level `protocolVersion` and `url`), while **Agent Registry** release notes
advertise v1.0 `supportedInterfaces`. Serving both dialects turned this from a
rebuild into a query parameter: register with
`/.well-known/agent-card.json?dialect=0.3` and let the runtime answer v1.0
callers natively.

**Design that paid for itself:** one internal representation, dialect
conversion only at the edges, the request's method name deciding which dialect
the reply is rendered in. Business logic is not forked. If you only ever need
one dialect, deleting the other is a deletion rather than a rewrite.

### Registration mechanics

- A2A is the **only** registration type for Cloud Run and GKE (there is no
  reasoning engine to invoke natively). ADK registration is for Agent Runtime.
- `agents-cli publish gemini-enterprise --agent-card-url … --gemini-enterprise-app-id …`
  is idempotent: re-running updates in place.
- The platform stores a **snapshot** of the card
  (`a2aAgentDefinition.jsonAgentCard`), not a live reference. Changing the
  served card does nothing until publish is re-run.
- `agents-cli publish gemini-enterprise --list` prints app resource names in the
  exact format the `--gemini-enterprise-app-id` flag wants.
- The card's `description` is what the assistant uses for routing, so state what
  the agent does **and what it does not** — otherwise the assistant reaches for
  the only KYC-sounding agent it has.

### Structured input beats parsed prose

An A2A caller may send a data part or a sentence. A01 prefers a data part
(`{"pan": …, "fullName": …}`) and falls back to a deliberately dumb filter for
text: a fixed regex for the PAN (whose structure is fixed, so this is reliable)
and a filler-word filter for the name. It is a filter, not a parser —
`"…for R. K. Sharma who applied today"` yields the trailing clause too. When
either field cannot be determined it returns `input-required` naming what is
missing rather than guessing a name into a KYC decision. Extraction sloppiness
then degrades into a low match score, never a false accept.

---

## 7. Working with parallel subagents

Six agents built A01's modules concurrently in about ten minutes. What made it
work, and what broke.

**Specify exact interfaces up front.** Every prompt carried the dataclass or
function signature the other agents were coding against. Without that, six
plausible-but-incompatible APIs.

**One owner per file, named explicitly.** Each agent got a list of files it may
create or modify and was told to touch nothing else — including
`pyproject.toml`, `uv.lock`, and `tests/conftest.py` (assigned to exactly one
agent, since fixtures are shared infrastructure).

**Ask for measured tables, not assurances.** Requiring each report to include
actual scores surfaced the transliteration weakness (`Laxmi`/`Lakshmi` = 0.51)
immediately, which is what drove the clamp recalibration later.

**Error: committing while workers were still writing.** A `git add -A` mid-run
captured an in-progress `matching.py`; the agent's later refinements landed
afterwards. Nothing was lost, but the commit was a lie about a moment that
never existed. Commit after workers report, not while they run.

**Let them overrule you.** One agent rejected my instruction on the v0.3
stream-event literals (`taskStatusUpdate`) in favour of `status-update`, citing
the migration page, and was right — my instruction came from a lossy doc fetch.
Prompts should invite disagreement and require it to be flagged with evidence.

**My own bug, worth remembering:** I wrote a test using `ZZZZZ9999Z` as a
"well-formed but unknown" PAN. It isn't well-formed — position 4 encodes the
holder type and `Z` is not a valid code, so it failed structural validation and
the test asserted the wrong branch. Generate fixtures through the same
validator the code uses.

---

## 8. Compliance content

The `explain` block is the reason these contracts exist, so the citations in it
have to be defensible.

- **Hedge locators you cannot verify.** `common/policy.py` uses
  `Chapter VI (Customer Due Diligence)` where the exact section is uncertain,
  rather than inventing `Section 23(4)(b)`. Confident locators (PMLA s.12, IT
  Act s.139A, IT Rules 114B, PML Rules Rule 9, DPDP s.5) are stated; the rest
  are flagged in the module docstring for a compliance reviewer.
- **A domain error caught by writing it down:** the brief described PAN as an
  officially valid document. Under the RBI KYC Direction it is not — it is a
  separately mandated identity credential collected *alongside* an OVD. The
  catalog says so explicitly so no agent can cite it wrongly.
- **One catalog, stable IDs, shared across agents.** `ids_for_agent("A01")`
  returns the nine A01 cites; it raises for an unmapped agent, because an agent
  emitting zero citations is a compliance gap and should fail loudly.

---

## Checklist for the next agent

**Before writing code**
1. Confirm the deployment target (Cloud Run vs Agent Runtime) — they have
   different registration paths and different calling conventions.
2. Confirm the model and its residency implications for that agent's payload.
3. Write the contract as Pydantic models first; keep the platform's casing.

**While building**
4. Deterministic code owns every decision it can make offline; the model gets a
   narrow schema and never the envelope.
5. The entrypoint never raises — express failure inside the contract.
6. Reuse `common/`: envelope, PAN validation, policy catalog. Add to the catalog
   rather than inventing citation strings.
7. Test the contract guardrail with broken payloads, and lock any measured
   constant (thresholds, clamps, timeouts) in a test that calls the real code.
8. Budget for thinking latency; set the model timeout to seconds, not
   milliseconds-of-hope.

**Deploying**
9. Dedicated runtime service account with the single role it needs.
10. Don't advertise `/healthz` externally; add `/status`.
11. Check anything that advertises a URL *on the deployed instance*.
12. Service-scoped IAM bindings come after the first deploy.

**Registering**
13. Serve the A2A card in the shape the registration API accepts today (v0.3),
    even when the runtime speaks the latest spec.
14. Grant `roles/run.servicesInvoker` to the Discovery Engine service agent.
15. Write the card description to include what the agent does **not** do.
16. Re-run `agents-cli publish` after any card change — the platform holds a
    snapshot.
17. Nothing is proven until the platform itself routes a real query. Watch the
    logs on the first one.
