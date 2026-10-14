# syntax=docker/dockerfile:1
# souq-feeds: supplier feed upload + streaming import -> Postgres + transactional outbox.
# Build context is the repo root:  docker buildx bake feeds   (or)
#   docker build -f services/feeds/Dockerfile .
# services/feeds/Dockerfile.dockerignore whitelists the files this image needs; BuildKit prefers it over
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
    --mount=type=bind,source=services/feeds/pyproject.toml,target=services/feeds/pyproject.toml \
    uv sync --frozen --no-dev --no-install-workspace --package souq-feeds
COPY pyproject.toml uv.lock ./
COPY libs/py-common libs/py-common
COPY services/feeds services/feeds
# Workspace members stay editable (a .pth pointing at /src/...): the venv layer then depends only on
# manifests, and a code-only release ships just the small source layer below instead of the whole venv.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package souq-feeds \
 && python -m compileall -q -j0 libs/py-common/src services/feeds/src

FROM python:${PYTHON_VERSION}-slim-trixie AS runtime
# pip is unused at runtime and is the most common source of fixable CVEs in the base image.
RUN python -m pip uninstall --yes --quiet pip \
 && groupadd --system --gid 10001 souq \
 && useradd --system --uid 10001 --gid souq --home-dir /nonexistent --shell /usr/sbin/nologin souq \
 && install -d -o 10001 -g 10001 -m 0750 /var/lib/souq/feeds
COPY --from=builder /opt/venv /opt/venv
COPY config /app/config
COPY --from=builder /src/libs/py-common/src /src/libs/py-common/src
COPY --from=builder /src/services/feeds/src /src/services/feeds/src
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SOUQ_CONFIG_DIR=/app/config \
    HOST=0.0.0.0 \
    PORT=8002 \
    FEEDS_UPLOAD_DIR=/var/lib/souq/feeds
WORKDIR /app
# Numeric uid so Kubernetes runAsNonRoot can verify it without resolving /etc/passwd.
USER 10001:10001
EXPOSE 8002
# /healthz is dependency-free liveness; readiness (/readyz) is left to orchestrator probes.
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8002'), timeout=2)"]
ENTRYPOINT ["souq-feeds"]
