# TripWeaver — one image, two roles (API and Celery worker), distinguished
# only by the command each docker-compose service overrides (see
# docker-compose.yml). Building one image instead of two keeps the
# dependency set identical between the two processes -- they already have
# to agree on the same DB models/Celery task signatures to work together
# at all, so a version drift between "the API's dependencies" and "the
# worker's dependencies" would be a real, confusing failure mode to debug.

FROM python:3.12-slim AS base

# uv itself, copied from its own official image rather than pip-installed
# -- avoids needing pip/build tooling in the final image at all.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# libpq-dev: psycopg[binary] ships its own bundled libpq, but keeping this
# here is cheap insurance against a future dependency needing to compile
# against it. curl: used by the API service's own HEALTHCHECK.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency manifests first, install, THEN copy the rest of the
# source -- so an app-code-only change doesn't invalidate the (much
# slower) dependency-install layer on rebuild.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --extra api --extra agents --extra worker --extra mcp

COPY . .

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Overridden per-service in docker-compose.yml (the worker service runs
# celery instead) -- this default is the API process.
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
