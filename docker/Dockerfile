# syntax=docker/dockerfile:1.7
# Lithos Lens - project skeleton
# Multi-stage build for smaller image

# Stage 1: Build dependencies
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

RUN uv venv /app/.venv
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV UV_LINK_MODE=copy

# Cache dependency layer separately from source
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# Install the project separately so normal code changes reuse the cached dependency layer.
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install .

# Stage 2: Runtime image
FROM python:3.12-slim AS runtime

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Install runtime system packages here (none needed by the skeleton; keep
# the layer so downstream projects have an obvious place to extend).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Create runtime user and data directory with host-friendly ownership
RUN groupadd --gid 1000 lithos-lens \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/lithos-lens --shell /usr/sbin/nologin lithos-lens \
    && mkdir -p /data \
    && chown -R 1000:1000 /data /home/lithos-lens

# Default environment variables pointing to /data paths
ENV HOME=/home/lithos-lens
ENV LITHOS_LENS_CONFIG=/data/lithos-lens.toml
ENV LITHOS_LENS_DATA_DIR=/data

USER 1000:1000

CMD ["lithos-lens"]
