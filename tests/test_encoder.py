"""The encoder must reach the configured gateway, and must never return a short result.

This replaces scripts/probe_encoder.py, whose question was whether OPENAI_API_BASE was read from the
environment, because upstream's Encoder dropped the api_base argument and left that as the only
route to a gateway. It takes the argument now, so the question is answerable here instead of by a
script somebody has to remember to run.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import numpy as np
import pytest

from kvasir.storm import runtime
from kvasir.storm.encoder import EmbeddingError, Encoder, cosine_similarity

DIMENSIONS = 4


class _Sink:
    """The usage sink the service installs, reduced to what these assert on."""

    def __init__(self) -> None:
        self.embeddings: list[tuple[str, int]] = []
        self._lock = threading.Lock()

    def record_lm(
        self, model: str, role: str | None, prompt_tokens: int, completion_tokens: int, cost: float
    ) -> None:
        pass

    def record_embedding(self, model: str, tokens: int) -> None:
        # Called from litellm's logging thread, so this is the one that needs a lock.
        with self._lock:
            self.embeddings.append((model, tokens))

    def record_search(self, engine: str, queries: int) -> None:
        pass


class _Gateway:
    """A stand-in for the gateway that records what reached it."""

    def __init__(self, embeddings_per_request: int | None = None, status: int = 200) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._embeddings_per_request = embeddings_per_request
        self._status = status
        probe = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                probe.requests.append((self.path, body))

                if probe._status != 200:
                    self.send_response(probe._status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                inputs = body.get("input")
                count = probe._embeddings_per_request
                if count is None:
                    count = len(inputs) if isinstance(inputs, list) else 1

                payload = json.dumps(
                    {
                        "object": "list",
                        "model": body.get("model", "unknown"),
                        "data": [
                            {
                                "object": "embedding",
                                "index": index,
                                "embedding": [float(index)] * DIMENSIONS,
                            }
                            for index in range(count)
                        ],
                        "usage": {"prompt_tokens": 7, "total_tokens": 7},
                    }
                ).encode()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)

    def __enter__(self) -> _Gateway:
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}/v1"


@pytest.fixture
def gateway() -> Iterator[_Gateway]:
    with _Gateway() as running:
        yield running


def test_requests_reach_the_configured_base_and_model(gateway: _Gateway) -> None:
    encoder = Encoder(model="openai/my-embedding-model", api_key="k", api_base=gateway.base_url)

    encoder.encode("a topic")

    assert len(gateway.requests) == 1
    path, body = gateway.requests[0]
    assert path == "/v1/embeddings"
    # dspy routes on the leading segment and consumes it, so an embedding name is prefixed the same
    # way a language model's is: "openai/ollama/model:cloud" arrives as "ollama/model:cloud".
    assert body["model"] == "my-embedding-model"


def test_a_list_is_one_request(gateway: _Gateway) -> None:
    """Upstream issued one request per text across a thread pool."""
    encoder = Encoder(model="openai/m", api_key="k", api_base=gateway.base_url)

    encoder.encode(["one", "two", "three"])

    assert len(gateway.requests) == 1
    assert gateway.requests[0][1]["input"] == ["one", "two", "three"]


def test_a_list_returns_a_row_per_text(gateway: _Gateway) -> None:
    """Rows are the response's own order. dspy reads `data` as it arrives and does not sort by
    `index`, so a provider that answers out of order is beyond what this can check; the count is
    what catches the failure upstream actually had."""
    encoder = Encoder(model="openai/m", api_key="k", api_base=gateway.base_url)

    vectors = encoder.encode(["one", "two", "three"])

    assert vectors.shape == (3, DIMENSIONS)


def test_a_single_text_returns_one_dimension(gateway: _Gateway) -> None:
    encoder = Encoder(model="openai/m", api_key="k", api_base=gateway.base_url)

    assert encoder.encode("just one").shape == (DIMENSIONS,)


def test_a_short_response_raises_rather_than_misaligning() -> None:
    """Upstream logged the failures, returned a short array, and left rows against wrong texts."""
    with _Gateway(embeddings_per_request=2) as short:
        encoder = Encoder(model="openai/m", api_key="k", api_base=short.base_url)

        with pytest.raises(EmbeddingError, match="returned 2 embeddings for 3"):
            encoder.encode(["one", "two", "three"])


def test_a_failed_request_raises() -> None:
    with _Gateway(status=500) as failing:
        encoder = Encoder(model="openai/m", api_key="k", api_base=failing.base_url)

        with pytest.raises(EmbeddingError):
            encoder.encode(["one"])


def test_tokens_reach_the_run_that_asked_for_them(gateway: _Gateway) -> None:
    """dspy discards the usage object, so this arrives through litellm's success hook — which runs
    on litellm's own logging thread, after the call returned. The sink travels with the request
    because a contextvar would not survive that hop."""
    encoder = Encoder(model="openai/m", api_key="k", api_base=gateway.base_url)
    sink = _Sink()

    with runtime.record_usage_into(sink):
        encoder.encode(["one"])
        encoder.encode(["two"])

    deadline = time.monotonic() + 5
    while len(sink.embeddings) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)

    assert sink.embeddings == [("m", 7), ("m", 7)]


def test_tokens_are_not_reported_outside_a_run(gateway: _Gateway) -> None:
    """No sink installed is the library case, and must not raise on the logging thread."""
    encoder = Encoder(model="openai/m", api_key="k", api_base=gateway.base_url)

    assert encoder.encode(["one"]).shape == (1, DIMENSIONS)


def test_cosine_similarity_matches_the_shape_callers_index() -> None:
    corpus = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])

    similarities = cosine_similarity([np.array([1.0, 0.0])], corpus)[0]

    assert similarities.shape == (3,)
    assert similarities[0] == pytest.approx(1.0)
    assert similarities[1] == pytest.approx(0.0)
    assert similarities[2] == pytest.approx(0.7071, abs=1e-4)


def test_cosine_similarity_tolerates_a_zero_vector() -> None:
    similarities = cosine_similarity([np.array([0.0, 0.0])], np.array([[1.0, 0.0]]))

    assert similarities[0][0] == 0.0
