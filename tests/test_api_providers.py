"""Listing the providers and their models, with each provider faked."""

import httpx
import pytest
from fastapi.testclient import TestClient

from fakes import AUTH, ENVIRONMENT, STORED, store_settings
from kvasir.api import providers
from kvasir.api.app import app

OLLAMA = "http://ollama.example/v1"
OTHER = "http://router.example/api/v1"
OLLAMA_MODELS = [{"id": "qwen:8b"}, {"id": "embed:v1"}]


@pytest.fixture
def requests(monkeypatch, tmp_path):
    """Every request a provider is sent. ollama is Ollama, router lists with context lengths, and
    dead refuses."""
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KVASIR_PROVIDER_OLLAMA_API_BASE", OLLAMA)
    monkeypatch.setenv("KVASIR_PROVIDER_ROUTER_API_BASE", OTHER)
    monkeypatch.setenv("KVASIR_PROVIDER_ROUTER_API_KEY", "router-key")
    monkeypatch.setenv("KVASIR_PROVIDER_DEAD_API_BASE", "http://dead.example/v1")
    monkeypatch.setenv("KVASIR_DATA_DIR", str(tmp_path))
    store_settings(tmp_path, STORED)
    monkeypatch.setattr(providers, "_cache", {})
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        url = str(request.url)
        if url == f"{OLLAMA}/models":
            return httpx.Response(200, json={"data": OLLAMA_MODELS})
        if url == "http://ollama.example/api/version":
            return httpx.Response(200, json={"version": "0.40.13"})
        if url == "http://ollama.example/api/show":
            model = request.read().decode()
            if "broken" in model:
                return httpx.Response(500)
            if "odd" in model:
                info = {"general.architecture": "odd", "odd.context_length": True}
                return httpx.Response(200, json={"capabilities": ["tools", 3], "model_info": info})
            if "embed" in model:
                info = {"general.architecture": "bert", "bert.context_length": 2048}
                return httpx.Response(200, json={"capabilities": ["embedding"], "model_info": info})
            info = {
                "general.architecture": "qwen",
                "qwen.context_length": 40960,
                "qwen.rope.scaling.original_context_length": 4096,
            }
            return httpx.Response(200, json={"capabilities": ["completion"], "model_info": info})
        if url == f"{OTHER}/models":
            data = [{"id": f"org/m{i}", "context_length": 1000 + i} for i in range(3)]
            return httpx.Response(200, json={"data": data})
        if url.startswith("http://router.example"):
            return httpx.Response(404)
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(providers, "_client", lambda: httpx.AsyncClient(transport=transport))
    return seen


@pytest.fixture
def client(requests):
    with TestClient(app, headers=AUTH) as client:
        yield client


def test_providers_are_listed_by_name_with_whether_they_answer(client):
    assert client.get("/v1/providers").json() == [
        {"name": "dead", "reachable": False},
        {"name": "gateway", "reachable": False},
        {"name": "ollama", "reachable": True},
        {"name": "router", "reachable": True},
    ]


def test_ollama_models_carry_their_capabilities_and_trained_window(client):
    assert client.get("/v1/providers/ollama/models").json() == [
        {
            "id": "embed:v1",
            "ref": "ollama/embed:v1",
            "context_length": 2048,
            "capabilities": ["embedding"],
        },
        {
            "id": "qwen:8b",
            "ref": "ollama/qwen:8b",
            "context_length": 40960,
            "capabilities": ["completion"],
        },
    ]


def test_another_provider_is_asked_for_no_details_and_sent_its_key(client, requests):
    models = client.get("/v1/providers/router/models").json()

    assert [(m["ref"], m["context_length"], m["capabilities"]) for m in models] == [
        ("router/org/m0", 1000, None),
        ("router/org/m1", 1001, None),
        ("router/org/m2", 1002, None),
    ]
    assert [r.url.path for r in requests] == ["/api/v1/models", "/api/api/version"]
    assert {r.headers["authorization"] for r in requests} == {"Bearer router-key"}


def test_a_list_is_cached(client, requests):
    client.get("/v1/providers/ollama/models")
    asked = len(requests)

    client.get("/v1/providers/ollama/models")

    assert len(requests) == asked


def test_an_unknown_or_unreachable_provider_is_an_error(client):
    unknown = client.get("/v1/providers/nope/models")
    dead = client.get("/v1/providers/dead/models")

    assert (unknown.status_code, unknown.json()) == (404, {"message": "no provider nope"})
    assert (dead.status_code, dead.json()) == (
        502,
        {"message": "provider dead did not list its models"},
    )


def test_odd_answers_lose_only_their_odd_fields(client, monkeypatch):
    monkeypatch.setattr(
        "test_api_providers.OLLAMA_MODELS",
        [{"id": "odd:1", "context_length": "128k"}, {"id": 7}, "x", {"name": "no id"}],
    )

    assert client.get("/v1/providers/ollama/models").json() == [
        {"id": "odd:1", "ref": "ollama/odd:1", "context_length": None, "capabilities": None}
    ]


def test_a_list_missing_a_model_it_failed_to_ask_is_not_cached(client, requests, monkeypatch):
    monkeypatch.setattr("test_api_providers.OLLAMA_MODELS", [{"id": "qwen:8b"}, {"id": "broken:1"}])

    models = client.get("/v1/providers/ollama/models").json()
    asked = len(requests)
    client.get("/v1/providers/ollama/models")

    assert [m["capabilities"] for m in models] == [None, ["completion"]]
    assert len(requests) > asked


def test_an_empty_list_is_no_error(client, monkeypatch):
    monkeypatch.setattr("test_api_providers.OLLAMA_MODELS", [])

    assert client.get("/v1/providers/ollama/models").json() == []
