# Development tasks. Start with `just setup`, then `just dev`.

set shell := ["bash", "-euo", "pipefail", "-c"]

# Pinned like the images in the Dockerfile, so a pull cannot change what a run searches.
searxng_image := "docker.io/searxng/searxng:latest@sha256:e2ee9d55b73ce5b531b3d7b9aca3578c7517b3762a2a6153bee28b2645dda51e"

# Cargo features for the service, as in `just features=chromium dev`.
features := ""
cargo_features := if features == "" { "" } else { "--features " + features }

# In the recipes that run the service only: the tests must not inherit a real provider from .env.
load := 'if [ ! -f .env ]; then echo "no .env: run just setup" >&2; exit 1; fi; set -a; . ./.env; set +a'
runtime := 'runtime=$(command -v podman || command -v docker) || { echo "needs podman or docker" >&2; exit 1; }'

default:
    @just --list

# Create .env and settings.dev.json if missing, and install the dependencies
setup:
    #!/usr/bin/env bash
    set -euo pipefail
    if [ -f .env ]; then
        echo ".env exists, keeping it"
    else
        (
            umask 077
            sed -e "s/^KVASIR_TOKEN=$/KVASIR_TOKEN=$(openssl rand -hex 32)/" \
                -e "s/^KVASIR_SECRET_KEY=$/KVASIR_SECRET_KEY=$(openssl rand -hex 32)/" \
                -e "s/^SEARXNG_SECRET=$/SEARXNG_SECRET=$(openssl rand -hex 32)/" \
                env.example > .env
        )
        echo "created .env. Keep KVASIR_SECRET_KEY: the data directory opens only with it"
    fi
    [ -f settings.dev.json ] || cp settings.dev.example.json settings.dev.json
    pnpm --dir clients/web install --frozen-lockfile
    uv sync --locked

# Report which tools and services are missing
doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    failed=0
    for tool in rustup cargo cargo-nextest cargo-deny node pnpm uv openssl curl jq; do
        command -v "$tool" >/dev/null || { echo "missing: $tool"; failed=1; }
    done
    command -v podman >/dev/null || command -v docker >/dev/null \
        || { echo "missing: podman or docker"; failed=1; }
    [ "$(node --version 2>/dev/null | cut -d. -f1)" = v26 ] || { echo "needs Node 26"; failed=1; }
    [ -f .env ] || { echo "missing: .env, run just setup"; failed=1; }
    if [ -f .env ]; then set -a; . ./.env; set +a; fi
    base=${KVASIR_PROVIDER_LOCAL_API_BASE:-http://localhost:11434/v1}
    curl -fsS -m 2 "$base/models" >/dev/null 2>&1 || echo "not answering: provider at $base"
    curl -fsS -m 2 'http://localhost:8888/search?q=test&format=json' >/dev/null 2>&1 \
        || echo "not answering: SearXNG, just searxng starts it"
    [ "$failed" = 0 ] && echo "tools ok"
    exit "$failed"

# Run SearXNG, API and page together until Ctrl-C. SearXNG is left running
dev: searxng
    #!/usr/bin/env bash
    set -euo pipefail
    {{ load }}
    cargo build {{ cargo_features }}
    # Each job gets its own process group, so stopping one stops what it started.
    set -m
    groups=()
    stop() { for group in "${groups[@]}"; do kill -- "-$group" 2>/dev/null || true; done; }
    trap stop EXIT
    trap 'exit 130' INT TERM
    start_api() {
        target/debug/kvasir serve &
        api=$!
        groups+=("$api")
        until curl -fsS -m 2 "localhost:$KVASIR_PORT/healthz" >/dev/null 2>&1; do
            kill -0 "$api" 2>/dev/null || { echo "kvasir did not start" >&2; exit 1; }
            sleep 0.2
        done
    }
    start_api
    # Only a first start, so a setting changed on the page is not overwritten.
    if curl -sS "localhost:$KVASIR_PORT/readyz" | jq -e '.setup == "required"' >/dev/null; then
        seeded=$(just seed) || { echo "settings.dev.json refused: $seeded" >&2; exit 1; }
        # A setting read only at startup, such as runs.store, needs the service started again.
        if [ "$(jq '.restart_required | length' <<<"$seeded")" != 0 ]; then
            kill "$api"
            wait "$api" || true
            start_api
        fi
    fi
    pnpm --dir clients/web dev --strictPort &
    groups+=("$!")
    echo
    echo "page:  http://localhost:5173"
    echo "token: $KVASIR_TOKEN"
    echo
    wait -n

# Run only the API
api:
    @{{ load }}; cargo run {{ cargo_features }} -- serve

# Run only the page, with /v1 proxied to the API
web:
    @{{ load }}; pnpm --dir clients/web dev

# Run only the page, its API answered from the fixtures
mock:
    pnpm --dir clients/web dev:mock

# Start SearXNG on localhost:8888
searxng:
    #!/usr/bin/env bash
    set -euo pipefail
    {{ load }}
    {{ runtime }}
    if "$runtime" inspect kvasir-searxng >/dev/null 2>&1; then
        "$runtime" start kvasir-searxng >/dev/null
    else
        "$runtime" run -d --name kvasir-searxng -p 127.0.0.1:8888:8080 \
            -e SEARXNG_SECRET -e FORCE_OWNERSHIP=false \
            -v ./searxng/settings.yml:/etc/searxng/settings.yml:ro,Z \
            {{ searxng_image }} >/dev/null
    fi

# Stop and remove SearXNG
searxng-stop:
    @{{ runtime }}; "$runtime" rm -f kvasir-searxng >/dev/null

# Store settings.dev.json in the running service
seed:
    @{{ load }}; curl -sS --fail-with-body -X PATCH "localhost:$KVASIR_PORT/v1/settings" \
        -H "Authorization: Bearer $KVASIR_TOKEN" -H 'Content-Type: application/json' \
        --data @settings.dev.json

# Print the token to log in with
token:
    @{{ load }}; echo "$KVASIR_TOKEN"

# Format the Rust and the Python
fmt:
    cargo fmt
    uv run ruff format .

# Run the linters CI runs
lint:
    cargo fmt --check
    cargo clippy --locked --all-targets -- -D warnings
    cargo deny check
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    pnpm --dir clients/web check

# Run the Rust, contract and page unit tests
test: test-rust test-contract test-web

test-rust:
    cargo nextest run --locked --no-tests=pass

test-contract:
    cargo build --locked --bin kvasir
    uv run pytest

test-web:
    pnpm --dir clients/web test

# Run the Playwright tests
e2e:
    pnpm --dir clients/web exec playwright install chromium
    pnpm --dir clients/web e2e

# Generate the page's types from the service's schema
types:
    pnpm --dir clients/web types

# Run what CI runs, but for the image build
check: lint test types e2e
    git diff --exit-code clients/web/src/lib/schema.d.ts

# Build the page, then the release binary that embeds it
build:
    pnpm --dir clients/web build
    cargo build --locked --release {{ cargo_features }}

# Build the image
image:
    @{{ runtime }}; "$runtime" build -t kvasir:dev .

# Evaluate research into a directory, as in `just eval data/eval/baseline --method graph`
[positional-arguments]
eval *args:
    @{{ load }}; cargo run --release -- eval "$@"

# Delete the data directory
[confirm("Delete the data directory and everything in it?")]
reset:
    @{{ load }}; rm -rf -- "$KVASIR_DATA_DIR"

# Delete the build output
clean:
    cargo clean
