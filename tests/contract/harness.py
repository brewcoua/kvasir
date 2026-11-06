"""The service as a client sees it: a child process, and fakes for what it talks to over HTTP.

`KVASIR_CONTRACT_COMMAND` names the service to test; the default is this checkout's debug build,
which `cargo build --bin kvasir` makes. The harness starts it, rather than taking a URL, because the
service has to be pointed at the fakes when it starts.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

TOKEN = "contract-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
COST = 0.001
# Loopback, since the fakes listen on this machine.
BUILT = shlex.quote(str(Path(__file__).parents[2] / "target" / "debug" / "kvasir"))
COMMAND = os.environ.get("KVASIR_CONTRACT_COMMAND", f"{BUILT} serve --allow-loopback")
START_TIMEOUT_SECONDS = 60.0
FACT = "The Kvasir stone was catalogued in 1783 at the parish of Rok."
PAGE = (
    "<html><head><title>The Kvasir stone</title></head><body><main><h1>The Kvasir stone</h1>"
    f"<p>{FACT} Its runes were read much later by a visiting scholar, who wrote at length about "
    "the "
    "carving, the local granite it was cut from, and the people who raised it near the old road. "
    "Parish records from the same decade describe the stone standing beside the church wall, and a "
    "survey made a century afterwards measured it at just over two metres tall.</p>"
    '<p>See <a href="/web/other.html">the other page</a>.</p>'
    "</main></body></html>"
)
OTHER = PAGE.replace("The Kvasir stone</h1>", "Another account</h1>")
# A smallest PNG: what the service reads the type of an icon from is its bytes.
ICON = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


def _vector(text: str) -> list[float]:
    """A bag of hashed words, so a text is close to one that shares its words, as a model's is."""
    vector = [0.0] * 64
    for word in re.findall(r"\w+", text.lower()):
        vector[hashlib.sha256(word.encode()).digest()[0] % 64] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _output_fields(system: str) -> dict[str, str]:
    """The fields a prompt asks for and their types, as dspy's ChatAdapter lists them."""
    listed = system.partition("Your output fields are:")[2].partition("All interactions")[0]
    return dict(re.findall(r"^\d+\. `(\w+)` \((.+?)\)", listed, re.MULTILINE))


def scripted_reply(system: str, user: str) -> dict[str, Any]:
    """A valid answer to any signature, by the fields it asks for. A run ends quickly on these."""
    fields = _output_fields(system)
    claim = {"claim": "The stone was catalogued in 1783.", "quote": FACT, "confidence": "high"}
    named = {"title": "Origin", "question": "When was the Kvasir stone catalogued?"}
    section = {"title": "Origin", "questions": [1], "subsections": []}
    values: dict[str, Any] = {
        "reasoning": "One step at a time.",
        "brief": "Find when the Kvasir stone was catalogued.",
        "assumptions": ["Taken to mean the runestone."],
        "output": "report",
        "criteria": [],
        "entities": [],
        "queries": ["kvasir stone catalogued"],
        "query": "kvasir stone catalogued",
        "findings": [claim],
        "status": "answered",
        "claims": [1],
        "missing": "",
        "follow_ups": [],
        "raised": [],
        "finding": "Catalogued in 1783 [1].",
        "ranking": [],
        "text": "The stone was catalogued in 1783 [1].",
        "points": ["Catalogued in 1783 [1]."],
        "verdict": "supported",
        "reason": "the quote says so",
        "rewrite": "",
        "disagree": [],
        "met": True,
        "related_topics": [],
        "personas": ["Historian: studies the parish records"],
        "question": "When was the stone catalogued?",
        "answer": "The stone was catalogued in 1783 [1].",
        "outline": "# Origin\n## Discovery",
        "written_section": "# Origin\n## Discovery\nThe stone was catalogued in 1783 [1].",
        "lead_section": "The Kvasir stone is a runestone [1].",
        "page": "# Origin\n## Discovery\nThe stone was catalogued in 1783 [1].",
    }
    if fields.get("criteria", "").startswith("list[Chosen"):
        values["criteria"] = [{"name": "age", "weight": 1}]
    if "entities" in fields and "brief" not in fields:
        values["entities"] = ["Kvasir stone"]
    # Two signatures answer `questions`, and two `sections`, each with their own type.
    values["questions"] = (
        [named] if "NamedQuestion" in fields.get("questions", "") else ["Which stone do you mean?"]
    )
    values["sections"] = [section] if "Section" in fields.get("sections", "") else ["Origin"]
    if "[[ ## question ## ]]" in user and "clear" in user.lower() and "questions" in fields:
        values["questions"] = []
    return {name: values[name] for name in fields}


class Fakes:
    """One server standing in for a provider, an Ollama provider, SearXNG, a site, an icon host."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        # Set to make a chat call wait here, so a run can be held while a test looks at it.
        self.hold: threading.Event | None = None
        self.held = threading.Event()
        # Answered in place of a chat call while set.
        self.chat_status: int | None = None
        # Answered in place of a page while set.
        self.web_status: int | None = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @contextmanager
    def holding_chat(self) -> Iterator[None]:
        """Chat calls wait until the block ends."""
        self.hold = threading.Event()
        self.held.clear()
        try:
            yield
        finally:
            self.hold.set()
            self.hold = None

    def environment(self) -> dict[str, str]:
        return {
            "KVASIR_PROVIDER_GATEWAY_API_BASE": f"{self.url}/gw/v1",
            "KVASIR_PROVIDER_GATEWAY_API_KEY": "provider-key",
            "KVASIR_PROVIDER_OLLAMA_API_BASE": f"{self.url}/ollama/v1",
            # Nothing listens here.
            "KVASIR_PROVIDER_DOWN_API_BASE": "http://127.0.0.1:9/v1",
        }

    def settings(self) -> dict[str, Any]:
        return {
            "model.fast": "gateway/fast",
            "model.strong": "gateway/strong",
            "model.embedding": "gateway/embed",
            "search.searxng.url": f"{self.url}/searx",
            "fetch.host_interval_seconds": 0,
            "fetch.render": "off",
            "favicons.providers": ["custom"],
            "favicons.custom_url": f"{self.url}/icon/{{domain}}",
            "runs.store": "sqlite",
            "runs.keep_call_content": True,
            "sources.enabled": True,
            "sources.wikidata": False,
            "sources.lists": [],
            "sources.deny": ["denied.example"],
        }


def _handler(fakes: Fakes) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send(
            self,
            status: int,
            body: bytes | dict[str, Any],
            content_type: str = "application/json",
            headers: dict[str, str] | None = None,
        ) -> None:
            data = json.dumps(body).encode() if isinstance(body, dict) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            fakes.requests.append((parts.path, parse_qs(parts.query)))
            if parts.path in ("/gw/v1/models", "/ollama/v1/models"):
                listed = [{"id": "strong", "context_length": 16000}, {"id": "fast"}, {"name": "x"}]
                self._send(200, {"data": listed})
            elif parts.path == "/ollama/api/version":
                self._send(200, {"version": "0.1"})
            elif parts.path == "/searx":
                self._send(
                    200,
                    {
                        "results": [
                            {
                                "url": f"{fakes.url}/web/stone.html",
                                "title": "The Kvasir stone",
                                "content": FACT,
                                "score": 1.0,
                            }
                        ]
                    },
                )
            elif parts.path.startswith("/web/") and fakes.web_status is not None:
                self._send(fakes.web_status, {"message": "scripted failure"})
            elif parts.path == "/web/stone.html":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif parts.path == "/web/other.html":
                self._send(200, OTHER.encode(), "text/html; charset=utf-8")
            elif parts.path.startswith("/icon/known.example"):
                self._send(200, ICON, "image/png")
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
            fakes.requests.append((self.path, body))
            if self.path == "/ollama/api/show":
                self._send(
                    200,
                    {
                        "capabilities": ["completion"],
                        "model_info": {
                            "general.architecture": "llama",
                            "llama.context_length": 8192,
                        },
                    },
                )
            elif self.path.endswith("/embeddings"):
                texts = body["input"] if isinstance(body["input"], list) else [body["input"]]
                self._send(
                    200,
                    {
                        "object": "list",
                        "model": body["model"],
                        "data": [
                            {"object": "embedding", "index": i, "embedding": _vector(text)}
                            for i, text in enumerate(texts)
                        ],
                        "usage": {"prompt_tokens": len(texts), "total_tokens": len(texts)},
                    },
                )
            elif self.path.endswith("/chat/completions"):
                self._chat(body)
            else:
                self._send(404, {"message": "not found"})

        def _chat(self, body: dict[str, Any]) -> None:
            if fakes.hold is not None:
                fakes.held.set()
                fakes.hold.wait(30)
            if fakes.chat_status is not None:
                self._send(fakes.chat_status, {"error": {"message": "scripted failure"}})
                return
            messages = body["messages"]
            system = messages[0]["content"] if messages[0]["role"] == "system" else ""
            if "Your output fields are:" in system:
                answer = scripted_reply(system, messages[-1]["content"])
                content = "\n\n".join(
                    f"[[ ## {name} ## ]]\n{value if isinstance(value, str) else json.dumps(value)}"
                    for name, value in answer.items()
                )
                content += "\n\n[[ ## completed ## ]]"
            else:
                content = "A title"
            self._send(
                200,
                {
                    "id": "chat",
                    "object": "chat.completion",
                    "created": 0,
                    "model": body["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                },
                headers={"x-litellm-response-cost": str(COST)},
            )

    return Handler


class Service:
    """A service on its own port and data directory, started and stopped as a deployment would."""

    def __init__(self, data: Path, environment: dict[str, str]) -> None:
        self.data = data
        self.environment = environment
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[bytes] | None = None
        self.log = data.parent / f"{data.name}.log"

    def client(self, **kwargs: Any) -> httpx.Client:
        kwargs.setdefault("headers", AUTH)
        return httpx.Client(base_url=self.url, timeout=60.0, **kwargs)

    def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("KVASIR_")}
        env |= self.environment | {
            "KVASIR_TOKEN": TOKEN,
            "KVASIR_SECRET_KEY": "ab" * 32,
            "KVASIR_DATA_DIR": str(self.data),
            "KVASIR_HOST": "127.0.0.1",
            "KVASIR_PORT": str(self.port),
            # One at a time, so what a run does happens in one order.
            "KVASIR_MAX_THREADS": "1",
        }
        output = self.log.open("ab")
        self.process = subprocess.Popen(
            shlex.split(COMMAND), env=env, stdout=output, stderr=subprocess.STDOUT
        )
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                if httpx.get(f"{self.url}/healthz", timeout=1.0).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.1)
        raise RuntimeError(f"the service did not start:\n{self.log.read_text()[-4000:]}")

    def stop(self) -> None:
        if self.process is not None:
            self.process.terminate()
            self.process.wait(30)
            self.process = None

    def kill(self) -> None:
        """As a crash would, leaving behind whatever was running."""
        if self.process is not None:
            self.process.kill()
            self.process.wait(30)
            self.process = None

    def restart(self) -> None:
        self.stop()
        self.start()


def frames(body: Iterator[str]) -> Iterator[tuple[str, Any]]:
    """(event, payload) pairs of an SSE body, read line by line. A blank line ends a frame."""
    event = ""
    for line in body:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            yield event, json.loads(line.removeprefix("data: "))


def transcript(client: httpx.Client, url: str, **kwargs: Any) -> list[tuple[str, Any]]:
    """Every frame of a stream that ends by itself."""
    method = kwargs.pop("method", "POST")
    with client.stream(method, url, **kwargs) as response:
        assert response.status_code == 200, response.read()
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        return list(frames(response.iter_lines()))


def shape(value: Any) -> Any:
    """What a value looks like: its keys all the way down, with each scalar reduced to a type.

    A contract on fields rather than on values, which a run's ids, times and wording would break.
    """
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        shapes = {json.dumps(shape(item), sort_keys=True) for item in value}
        return [json.loads(item) for item in sorted(shapes)]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    return "number" if isinstance(value, int | float) else "string"


SHAPES = Path(__file__).with_name("shapes.json")
_RECORD = bool(os.environ.get("KVASIR_CONTRACT_RECORD"))


def matches_shape(name: str, value: Any) -> None:
    """Compares with what `shapes.json` holds under `name`. Set `KVASIR_CONTRACT_RECORD` to write
    it instead, from the service that defines the contract."""
    found = shape(value)
    known = json.loads(SHAPES.read_text()) if SHAPES.exists() else {}
    if _RECORD:
        SHAPES.write_text(json.dumps(known | {name: found}, indent=1, sort_keys=True) + "\n")
        return
    assert found == known[name], name


def until(condition: Callable[[], Any], seconds: float = 30.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("the condition never held")
