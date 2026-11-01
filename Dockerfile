# Digests rather than tags, so a rebuild resolves to the same bytes and the published image can be
# pinned by digest downstream. Both are multi-arch indexes, so this builds for arm64 unchanged if
# the publishing workflow is ever asked to.
# The runs page, built into the package's static directory. Only its output reaches the image.
FROM docker.io/library/node:26.13.0-bookworm-slim@sha256:bc0a2afd3b74ce12222fc8bab99271e66257f021078f5f4a5d687a77d280c1d9 AS web

RUN npm install --global pnpm@12.15.0

WORKDIR /app/web

COPY web/package.json web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store

COPY web ./
RUN pnpm build


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


FROM docker.io/library/python:3.11-slim-bookworm@sha256:2e32f7d302adc1c37428355c1e646897c0c53f4fd60b6a551245fb90ee129f91

COPY --from=build /app/.venv /app/.venv
COPY src /app/src
COPY --from=web /app/src/kvasir/static /app/src/kvasir/static

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KVASIR_DATA_DIR=/data \
    DO_NOT_TRACK=1

WORKDIR /app

# Numeric so a Kubernetes runAsNonRoot check can tell this is not root without resolving a name.
USER 65532:65532

EXPOSE 8080

# Not `uvicorn kvasir.api.app:app`: the module configures logging before uvicorn can install its
# own, which is what keeps the access log in the same format as everything else. See
# kvasir/__main__.py.
CMD ["python", "-m", "kvasir"]
