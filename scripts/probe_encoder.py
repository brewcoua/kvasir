"""Determine whether the fork's Encoder can be pointed at an OpenAI-compatible gateway.

Co-STORM constructs `Encoder()` internally with no arguments, and `Encoder.__init__` drops
`api_base` for `encoder_type="openai"`. Redirecting embeddings away from api.openai.com therefore
depends on litellm reading `OPENAI_API_BASE` from the environment, which is litellm's behaviour
rather than a documented contract of upstream. If it does not hold, Co-STORM cannot run
against a gateway and only plain STORM is shippable.

The probe stands up a throwaway HTTP server on loopback, points `OPENAI_API_BASE` at it, and asks
the Encoder for one embedding. If the server receives the request, the environment was honoured and
the redirect works. Serving a valid response back also confirms the returned vector reaches the
caller intact, which a connection failure could not show.

This needs no gateway and no credentials, and it is stronger evidence than watching a request fail,
because the request is observed arriving rather than inferred from an error message.

Usage:
    uv run python scripts/probe_encoder.py
"""

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

EMBEDDING_DIMENSIONS = 4

received: list[tuple[str, dict[str, object]]] = []


class RecordingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        received.append((self.path, body))

        payload = json.dumps(
            {
                "object": "list",
                "model": body.get("model", "unknown"),
                "data": [
                    {
                        "object": "embedding",
                        "index": 0,
                        "embedding": [0.1] * EMBEDDING_DIMENSIONS,
                    }
                ],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            }
        ).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), RecordingHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_port}/v1"

    # Every one of these must be set before kvasir.storm.encoder is imported. That module
    # configures litellm and opens a disk cache under Path.home() at import time.
    os.environ["HOME"] = tempfile.mkdtemp(prefix="kvasir-probe-home-")
    os.environ["ENCODER_API_TYPE"] = "openai"
    os.environ["OPENAI_API_KEY"] = "probe-not-a-real-key"
    os.environ["OPENAI_API_BASE"] = base_url
    os.environ["OPENAI_BASE_URL"] = base_url

    from kvasir.storm.encoder import Encoder

    encoder = Encoder()
    print(f"gateway stand-in: {base_url}")
    print(f"embedding model:  {encoder.embedding_model_name}")
    print(f"kwargs passed:    {sorted(encoder.kargs)}")

    try:
        # caching=True would let a warm disk cache answer without a request, hiding the result.
        vector = encoder.encode("kvasir encoder probe")
    except Exception as exc:  # noqa: BLE001 - any failure is a result worth printing
        print(f"error: {type(exc).__name__}: {exc}")
        vector = None

    server.shutdown()

    if not received:
        print("VERDICT: IGNORED, no request arrived. Co-STORM is blocked.")
        return 1

    path, body = received[0]
    print(f"request path:     {path}")
    print(f"request model:    {body.get('model')}")
    print(f"vector returned:  {vector}")

    if vector is None or len(vector) != EMBEDDING_DIMENSIONS:
        print("VERDICT: PARTIAL, the request arrived but the response did not reach the caller.")
        return 1

    print("VERDICT: HONOURED, litellm used OPENAI_API_BASE. Co-STORM is in scope.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
