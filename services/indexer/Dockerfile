# syntax=docker/dockerfile:1
# souq-indexer: catalog.changes consumer -> OpenSearch bulk (AIMD backpressure, retry topics, DLQ).
# Build context is the repo root:  docker buildx bake indexer   (or)
#   docker build -f services/indexer/Dockerfile .
# services/indexer/Dockerfile.dockerignore whitelists the files this image needs; BuildKit prefers it over
# the root .dockerignore, so edits elsewhere in the monorepo do not invalidate these layers.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.11.14

FROM ghcr.io/astral-sh/uv:${UV_VERSION}-python${PYTHON_VERSION}-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Third-party deps only; reused until uv.lock or a manifest changes. --frozen (not --locked) because only
# this member's slice of the workspace is in the context, so uv cannot re-validate the whole lock here
# (CI's `uv sync --locked` does that).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=libs/py-common/pyproject.toml,target=libs/py-common/pyproject.toml \
    --mount=type=bind,source=services/indexer/pyproject.toml,target=services/indexer/pyproject.toml \
    uv sync --frozen --no-dev --no-install-workspace --package souq-indexer
COPY pyproject.toml uv.lock ./
COPY libs/py-common libs/py-common
COPY services/indexer services/indexer
# Workspace members stay editable (a .pth pointing at /src/...): the venv layer then depends only on
# manifests, and a code-only release ships just the small source layer below instead of the whole venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package souq-indexer \
 && python -m compileall -q -j0 libs/py-common/src services/indexer/src

# ONNX artefacts. Empty by default so local/dev builds stay small (compose mounts models/artifacts at
# /app/models instead); release.yml overrides this stage with the named build context `models` (bake var
# INDEXER_MODELS_DIR) only after every file is verified against models/checksums.txt.
FROM scratch AS models

FROM python:${PYTHON_VERSION}-slim-trixie AS runtime
# pip is unused at runtime and is the most common source of fixable CVEs in the base image.
RUN python -m pip uninstall --yes --quiet pip \
 && groupadd --system --gid 10001 souq \
 && useradd --system --uid 10001 --gid souq --home-dir /nonexistent --shell /usr/sbin/nologin souq
COPY --from=models / /app/models/
COPY --from=builder /opt/venv /opt/venv
COPY config /app/config
COPY --from=builder /src/libs/py-common/src /src/libs/py-common/src
COPY --from=builder /src/services/indexer/src /src/services/indexer/src
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOUQ_CONFIG_DIR=/app/config \
    INDEXER_HEALTH_PORT=8003 \
    SOUQ_EMBEDDER_MODEL=/app/models/bge-m3-int8 \
    HF_HUB_OFFLINE=1
WORKDIR /app
# Numeric uid so Kubernetes runAsNonRoot can verify it without resolving /etc/passwd.
USER 10001:10001
EXPOSE 8003
# Liveness from the health side-car (consume loop alive); readiness is /readyz.
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('INDEXER_HEALTH_PORT', '8003'), timeout=2)"]
ENTRYPOINT ["souq-indexer"]
