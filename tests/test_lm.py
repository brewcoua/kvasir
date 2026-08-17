"""The model wrapper must reach the gateway, report what a call spent, and retry only what a retry
can fix.

Same shape as tests/test_encoder.py: a real local HTTP server, because the point of these is what
goes over the wire now that no library sits in between.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import dspy
import pytest

from kvasir.storm import runtime
from kvasir.storm.lm import GatewayModel


class _Gateway:
    """A stand-in for the gateway that records what reached it."""

    def __init__(
        self,
        usage: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        statuses: list[int] | None = None,
    ) -> None:
        self.requests: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        self._usage = {"prompt_tokens": 11, "completion_tokens": 5} if usage is None else usage
        self._headers = headers or {}
        self._statuses = list(statuses or [])
        probe = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                probe.requests.append((self.path, body, dict(self.headers)))

                status = probe._statuses.pop(0) if probe._statuses else 200
                if status != 200:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                payload = json.dumps(
                    {
                        "choices": [{"message": {"role": "assistant", "content": "an answer"}}],
                        "usage": probe._usage,
                    }
                ).encode()

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in probe._headers.items():
                    self.send_header(name, value)
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


class _Sink:
    """The usage sink the service installs, reduced to what these assert on."""

    def __init__(self) -> None:
        self.lm: list[tuple[str, str | None, int, int, float]] = []

    def record_lm(
        self, model: str, role: str | None, prompt_tokens: int, completion_tokens: int, cost: float
    ) -> None:
        self.lm.append((model, role, prompt_tokens, completion_tokens, cost))

    def record_embedding(self, model: str, tokens: int) -> None:
        pass

    def record_search(self, engine: str, queries: int) -> None:
        pass


@pytest.fixture
def gateway() -> Iterator[_Gateway]:
    with _Gateway() as running:
        yield running


def _model(gateway: _Gateway, **kwargs: Any) -> GatewayModel:
    return GatewayModel(
        model="openai/ollama/strong:cloud",
        api_key="a-real-key",
        api_base=gateway.base_url,
        max_tokens=700,
        **kwargs,
    )


def test_a_call_reaches_chat_completions_with_the_routed_model_name(gateway: _Gateway) -> None:
    assert _model(gateway)(prompt="hello") == ["an answer"]

    path, body, headers = gateway.requests[0]
    assert path == "/v1/chat/completions"
    # dspy routes on the first segment and consumes it. Everything after it is the gateway's to
    # interpret, which is why the configured name carries an `openai/` prefix it never sees.
    assert body["model"] == "ollama/strong:cloud"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 700
    assert headers["Authorization"] == "Bearer a-real-key"


def test_credentials_are_not_sent_as_generation_parameters(gateway: _Gateway) -> None:
    _model(gateway)(prompt="hello")

    _, body, _ = gateway.requests[0]
    assert not [key for key in body if key.startswith("api_")]


def test_usage_reaches_the_sink_with_the_model_role(gateway: _Gateway) -> None:
    model = _model(gateway, role="article_gen")
    sink = _Sink()

    with runtime.record_usage_into(sink):
        model(prompt="hello")

    assert sink.lm == [("openai/ollama/strong:cloud", "article_gen", 11, 5, 0.0)]


def test_token_usage_accumulates_and_resets(gateway: _Gateway) -> None:
    model = _model(gateway)

    model(prompt="one")
    model(prompt="two")

    assert model.get_usage_and_reset() == {
        "openai/ollama/strong:cloud": {"prompt_tokens": 22, "completion_tokens": 10}
    }
    assert model.get_usage_and_reset() == {
        "openai/ollama/strong:cloud": {"prompt_tokens": 0, "completion_tokens": 0}
    }


@pytest.mark.parametrize(
    "headers, usage, expected",
    [
        # A LiteLLM proxy reports the cost in a response header.
        ({"x-litellm-response-cost": "0.0042"}, {}, 0.0042),
        ({}, {"cost": 0.0042}, 0.0042),
        # Bifrost splits it.
        ({}, {"cost": {"prompt_cost": 0.001, "completion_cost": 0.002}}, 0.003),
        # A gateway that reports nothing leaves it at zero, which is not the same as free.
        ({}, {}, 0.0),
    ],
)
def test_cost_is_read_from_whichever_shape_the_gateway_reports(
    headers: dict[str, str], usage: dict[str, Any], expected: float
) -> None:
    reported = {"prompt_tokens": 1, "completion_tokens": 1, **usage}
    with _Gateway(usage=reported, headers=headers) as g:
        sink = _Sink()
        with runtime.record_usage_into(sink):
            _model(g)(prompt="hello")

    assert sink.lm[0][4] == pytest.approx(expected)


def test_a_server_error_is_retried() -> None:
    with _Gateway(statuses=[500, 200]) as flaky:
        assert _model(flaky)(prompt="hello") == ["an answer"]

        assert len(flaky.requests) == 2


def test_a_bad_request_is_not_retried() -> None:
    """litellm's own retries would take a 400 four times over; ours must not."""
    with _Gateway(statuses=[400] * 4) as refusing:
        with pytest.raises(dspy.LMInvalidRequestError):
            _model(refusing)(prompt="hello")

        assert len(refusing.requests) == 1
