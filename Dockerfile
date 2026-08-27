# devTeam daemon — FastAPI + Python agents, port 4223.
# Build context: this directory. Built by root docker-compose.yml.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY daemon ./daemon
COPY agents ./agents
COPY config ./config
COPY tests ./tests

EXPOSE 4223

# Run the venv interpreter directly. `uv run` would re-sync at container start,
# which tries to build the project itself (it was synced with --no-install-project)
# and crash-loops the container — deps must be resolved at build time, not boot.
#
# docker.yaml (not local-test.yaml) binds 0.0.0.0, reaches ollama on the host,
# and carries no baked-in admin key — see that file's header.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app

CMD ["/app/.venv/bin/python", "-m", "daemon.main", "--config", "config/docker.yaml"]
