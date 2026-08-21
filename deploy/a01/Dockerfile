# A01 — Identity Verification Agent, container image for Cloud Run.
#
# The build context is the REPO ROOT, not this directory, because the image
# needs pyproject.toml, uv.lock and src/:
#
#   docker build -f deploy/a01/Dockerfile -t a01 .
#
# `deploy.sh` arranges the same context for `gcloud run deploy --source`.

# uv ships as a static binary, so copying it beats running an installer script:
# no curl and no network step in the final image's history.
FROM ghcr.io/astral-sh/uv:latest AS uv

FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv

# UV_COMPILE_BYTECODE precompiles site-packages so cold starts do not pay for
# it; PYTHONDONTWRITEBYTECODE stops the runtime attempting further writes,
# which the non-root user could not do anyway. UV_NO_CACHE keeps uv's download
# cache out of the image layers.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_CACHE=1
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/usr/src/app/.venv
ENV PATH=/usr/src/app/.venv/bin:$PATH
ENV PORT=8080

WORKDIR /usr/src/app

# Dependency layer first, and without the project itself, so that editing
# src/ reuses the cached third-party install. README.md rides along because
# pyproject.toml declares it and the project build reads it.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Source layer. --frozen keeps the lockfile authoritative: a stale uv.lock
# fails the build instead of silently resolving something else.
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Least privilege: nothing at runtime writes to the image.
RUN useradd --create-home --uid 1001 app && chown -R app:app /usr/src/app
USER app

# Documentation only — Cloud Run routes to $PORT regardless.
EXPOSE 8080

# Single worker: Cloud Run scales by adding instances, not processes. Shell
# form so $PORT expands; `exec` keeps uvicorn as PID 1 so it receives Cloud
# Run's SIGTERM directly and drains instead of being killed.
CMD exec uvicorn savings_flow.agents.a01_id_verification.service:app \
      --host 0.0.0.0 \
      --port "${PORT:-8080}" \
      --workers 1
