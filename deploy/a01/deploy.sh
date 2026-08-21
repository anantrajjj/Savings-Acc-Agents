#!/usr/bin/env bash
#
# Deploy A01 — Identity Verification Agent — to Cloud Run.
#
# Idempotent: `gcloud run deploy` creates the service on the first run and
# rolls out a new revision on every run after that. Re-running with unchanged
# source is safe; it just produces another revision.
#
# Every value below is overridable from the environment, e.g.
#   REGION=asia-south2 SERVICE=a01-id-verification-dev ./deploy/a01/deploy.sh
#
# Prerequisites (one-off, per project):
#   gcloud auth login
#   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
#       artifactregistry.googleapis.com --project=sandboxa1
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-sandboxa1}"
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-a01-id-verification}"

# gemini-3.7-flash is served from the global endpoint only, so the agent's
# location must be `global` even though the service itself runs in asia-south1.
GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-global}"
A01_MODEL="${A01_MODEL:-gemini-3.7-flash}"

CONTAINER_PORT="${CONTAINER_PORT:-8080}"
CPU="${CPU:-1}"
MEMORY="${MEMORY:-1Gi}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-4}"
# One model round-trip per request; low concurrency keeps a cold instance from
# queueing behind its own first call.
CONCURRENCY="${CONCURRENCY:-8}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-120}"

GCLOUD="${GCLOUD:-gcloud}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"

# `gcloud run deploy --source` builds with the Dockerfile at the *root* of the
# source directory and has no flag to point elsewhere, while the build needs
# the repo root as its context (pyproject.toml, uv.lock, src/). So stage the
# agent's Dockerfile at the root for the duration of the deploy and remove it
# afterwards, rather than committing a root Dockerfile that belongs to one of
# several agents.
STAGED_DOCKERFILE="${REPO_ROOT}/Dockerfile"
if [[ -e "${STAGED_DOCKERFILE}" ]]; then
  echo "error: ${STAGED_DOCKERFILE} already exists; refusing to overwrite it." >&2
  echo "       Move it aside (a previous run may have been interrupted)." >&2
  exit 1
fi
cp "${SCRIPT_DIR}/Dockerfile" "${STAGED_DOCKERFILE}"
trap 'rm -f "${STAGED_DOCKERFILE}"' EXIT

echo "Deploying ${SERVICE} to ${PROJECT_ID}/${REGION} (model ${A01_MODEL})..."

"${GCLOUD}" run deploy "${SERVICE}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --source="${REPO_ROOT}" \
  --port="${CONTAINER_PORT}" \
  --cpu="${CPU}" \
  --memory="${MEMORY}" \
  --min-instances="${MIN_INSTANCES}" \
  --max-instances="${MAX_INSTANCES}" \
  --concurrency="${CONCURRENCY}" \
  --timeout="${REQUEST_TIMEOUT}" \
  --ingress=all \
  --no-allow-unauthenticated \
  --set-env-vars="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${GOOGLE_CLOUD_LOCATION},A01_MODEL=${A01_MODEL}" \
  --startup-probe="httpGet.path=/healthz,httpGet.port=${CONTAINER_PORT},periodSeconds=5,timeoutSeconds=5,failureThreshold=12" \
  --liveness-probe="httpGet.path=/healthz,httpGet.port=${CONTAINER_PORT},periodSeconds=30,timeoutSeconds=5,failureThreshold=3" \
  --quiet

SERVICE_URL="$(
  "${GCLOUD}" run services describe "${SERVICE}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --format='value(status.url)'
)"

# The Cloud Run stand-in for Agent Engine's
# `.../reasoningEngines/NNNN:query` — same envelope, different host.
cat <<INFO

Deployed: ${SERVICE}
  Query endpoint : ${SERVICE_URL}/query
  Health check   : ${SERVICE_URL}/healthz

Register ${SERVICE_URL}/query with the Gemini Enterprise app. Smoke test:

  curl -sS -X POST "${SERVICE_URL}/query" \\
    -H "Authorization: Bearer \$(${GCLOUD} auth print-identity-token)" \\
    -H "Content-Type: application/json" \\
    -d '{"class_method":"query","input":{"input":{"pan":"ZZBPS1002B","fullName":"R. K. Sharma"}}}'

The PAN above is a fixture in the mocked registry (registered as
"Rajesh Kumar Sharma"), so it exercises the initials-vs-expanded name match
rather than returning pan_not_found_in_registry.
INFO
