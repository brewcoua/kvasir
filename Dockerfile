# Digests rather than tags, so a rebuild resolves to the same bytes and the published image can be
# pinned by digest downstream. Both are multi-arch indexes, so this builds for arm64 unchanged if
# the publishing workflow is ever asked to.
FROM docker.io/library/python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91 AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile alone, so editing source does not invalidate this layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# STORM's article generation loads paraphrase-MiniLM-L6-v2 through sentence-transformers. Baking
# the weights in keeps a run from reaching huggingface.co, and keeps it working under a read-only
# root filesystem where the cache could not be written. This is the only network access the build
# needs beyond the package index.
ENV HF_HOME=/opt/huggingface
RUN /app/.venv/bin/python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-MiniLM-L6-v2')"


FROM docker.io/library/python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91

COPY --from=build /app/.venv /app/.venv
COPY --from=build /opt/huggingface /opt/huggingface
COPY src /app/src

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KVASIR_DATA_DIR=/data

# kvasir.storm.encoder opens a litellm disk cache under Path.home() while being imported, before
# any of our code runs. /tmp is writable under a read-only root filesystem, and losing a cache on
# restart costs nothing.
ENV HOME=/tmp

# Encoder.__init__ raises without this. An upstream implementation detail rather than an operator's
# decision, so it is fixed here instead of left to the deployment.
ENV ENCODER_API_TYPE=openai

# Read the baked weights, never fetch. Offline mode also stops the hub writing to HF_HOME, which
# lives on a read-only layer.
ENV HF_HOME=/opt/huggingface \
    HF_HUB_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1

WORKDIR /app

# Numeric so a Kubernetes runAsNonRoot check can tell this is not root without resolving a name.
USER 65532:65532

EXPOSE 8080

# 0.0.0.0 binds inside the container only. Nothing here is exposed to a network by itself.
CMD ["uvicorn", "kvasir.main:app", "--host", "0.0.0.0", "--port", "8080"]
