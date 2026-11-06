# Digests rather than tags, so a rebuild resolves to the same bytes and the published image can be
# pinned by digest downstream. All are multi-arch indexes, so this builds for arm64 unchanged if
# the publishing workflow is ever asked to.
# The runs page. Only its output reaches the image, embedded in the binary.
FROM docker.io/library/node:26.13.0-bookworm-slim@sha256:bc0a2afd3b74ce12222fc8bab99271e66257f021078f5f4a5d687a77d280c1d9 AS web

RUN npm install --global pnpm@12.15.0

WORKDIR /app/web

COPY web/package.json web/pnpm-lock.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile --store-dir /pnpm/store

COPY web ./
RUN pnpm build


# The image's compiler is the one rust-toolchain.toml pins, which is left out so rustup does not
# fetch clippy and rustfmt for a build that needs neither.
FROM docker.io/library/rust:1.99.1-slim-bookworm@sha256:f2c004e3d2d8b547e8cf7ffbc29205385f4e71d23e53e4e383065db5e9f822cd AS build

WORKDIR /app

COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY --from=web /app/src/kvasir/static ./src/kvasir/static

# The target directory is a cache mount, so the binary is copied out in the same step.
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/app/target \
    cargo build --locked --release --bin kvasir \
    && cp target/release/kvasir /kvasir


FROM docker.io/library/debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS app

# The TLS client verifies against the system's roots.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /kvasir /usr/local/bin/kvasir

ENV KVASIR_DATA_DIR=/data

# Numeric so a Kubernetes runAsNonRoot check can tell this is not root without resolving a name.
USER 65532:65532

EXPOSE 8080

CMD ["kvasir", "serve"]


# `--target browser`: the same image with the `chromium` feature and Chromium, for pages that need
# JavaScript. Run it with `--init --ipc=host`, so Chromium does not run out of shared memory.
FROM build AS build-browser
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/app/target \
    cargo build --locked --release --bin kvasir --features chromium \
    && cp target/release/kvasir /kvasir

FROM app AS browser
USER root
RUN apt-get update \
    && apt-get install --yes --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build-browser /kvasir /usr/local/bin/kvasir
ENV CHROME=/usr/bin/chromium
USER 65532:65532

# Last, so a build without --target still gives the image without Chromium.
FROM app
