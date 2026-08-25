# One image serves both the API and the worker; they differ only by command.
# Keeping them identical means a job can never run against different code than
# the API that enqueued it.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app


# ---------------------------------------------------------------- builder ---
FROM base AS builder

# Manifests first: this layer is cached until a dependency actually changes,
# so day-to-day source edits skip dependency resolution entirely.
COPY pyproject.toml uv.lock ./
COPY packages/core/pyproject.toml packages/core/
COPY packages/provider-aws-ses/pyproject.toml packages/provider-aws-ses/
COPY packages/sdk-python/pyproject.toml packages/sdk-python/
COPY apps/api/pyproject.toml apps/api/
COPY apps/worker/pyproject.toml apps/worker/

# Workspace members must exist for the build backend to resolve them, even
# though their real sources arrive in the next step.
RUN mkdir -p packages/core/src/seskit_core \
             packages/provider-aws-ses/src/seskit_provider_aws_ses \
             packages/sdk-python/src/seskit \
             apps/api/src/seskit_api \
             apps/worker/src/seskit_worker \
    && touch packages/core/src/seskit_core/__init__.py \
             packages/provider-aws-ses/src/seskit_provider_aws_ses/__init__.py \
             packages/sdk-python/src/seskit/__init__.py \
             apps/api/src/seskit_api/__init__.py \
             apps/worker/src/seskit_worker/__init__.py

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

COPY . .

# Reinstall so the workspace packages carry their real sources, not the stubs.
# Workspace members install editable, so the Compose bind mounts over /app/apps
# and /app/packages feed straight into the running process and --reload works.
# A production image should add --no-editable here to stay self-contained.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ------------------------------------------------------------------ final ---
FROM base AS runtime

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 seskit

COPY --from=builder --chown=seskit:seskit /app /app

USER seskit

EXPOSE 8000

CMD ["uvicorn", "seskit_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
