"""The providers named in the environment, and the models each serves, for picking a model.

Every provider answers `GET {api_base}/models` with ids. An `/models` entry with `context_length`,
as OpenRouter's has, keeps it. A provider that answers `GET /api/version` as Ollama does is also
asked `POST /api/show` for each model, which gives its capabilities and the window it was trained
with. That window is a hint: the one served, such as Ollama's `num_ctx`, is often far smaller, and
`model.context_tokens` must be the served one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kvasir.config import Provider, Settings

logger = logging.getLogger("kvasir")

router = APIRouter()

TIMEOUT_SECONDS = 5.0
# Long enough that a settings page opening the list again asks nothing, short enough that a model
# pulled since shows up soon.
CACHE_SECONDS = 300.0

# Ollama answers one show at a time per model; this many keep a long list quick without a flood.
SHOW_CONCURRENCY = 8

# By provider name, since the providers are fixed for the process: when, and what was found.
_cache: dict[str, tuple[float, list[ModelInfo]]] = {}
_locks: dict[str, asyncio.Lock] = {}


class ProviderInfo(BaseModel):
    name: str
    # Whether its model list could be read.
    reachable: bool


class ModelInfo(BaseModel):
    id: str
    # What a model setting takes: `<provider>/<id>`.
    ref: str
    # The window the model was trained with, when the provider says. Not the one it is served with.
    context_length: int | None = None
    # Such as `completion`, `embedding`, `tools`, `thinking`, when the provider says.
    capabilities: list[str] | None = None


class ProviderError(Exception):
    """A provider's models cannot be listed, with the status to answer."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS)


@router.get("/v1/providers")
async def list_providers(http: Request) -> list[ProviderInfo]:
    """Every provider, by name. Neither its address nor its key is shown."""
    settings: Settings = http.app.state.settings

    async def reachable(name: str) -> bool:
        try:
            await _models(name, settings.providers[name])
        except ProviderError:
            return False
        return True

    names = sorted(settings.providers)
    found = await asyncio.gather(*(reachable(name) for name in names))
    return [ProviderInfo(name=name, reachable=ok) for name, ok in zip(names, found, strict=True)]


@router.get("/v1/providers/{name}/models", response_model=list[ModelInfo])
async def list_models(http: Request, name: str) -> list[ModelInfo] | JSONResponse:
    """The models `name` serves, sorted by id. Cached for a few minutes."""
    settings: Settings = http.app.state.settings
    if name not in settings.providers:
        return JSONResponse({"message": f"no provider {name}"}, status_code=404)
    try:
        return await _models(name, settings.providers[name])
    except ProviderError as exc:
        return JSONResponse({"message": str(exc)}, status_code=exc.status_code)


async def _models(name: str, provider: Provider) -> list[ModelInfo]:
    # One listing at a time per provider, so a page asking twice at once does not ask twice.
    async with _locks.setdefault(name, asyncio.Lock()):
        cached = _cache.get(name)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        async with _client() as client:
            headers = {"Authorization": f"Bearer {provider.api_key}"}
            try:
                response = await client.get(f"{provider.api_base}/models", headers=headers)
                response.raise_for_status()
                entries = response.json()["data"]
                listed = [_entry(name, entry) for entry in entries if isinstance(entry, dict)]
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                logger.warning("cannot list the models of provider %s: %s", name, exc)
                raise ProviderError(502, f"provider {name} did not list its models") from None
            models = [model for model in listed if model is not None]
            shown: list[dict[str, Any] | None] = []
            root = provider.api_base.removesuffix("/v1")
            if await _is_ollama(client, root, headers):
                limit = asyncio.Semaphore(SHOW_CONCURRENCY)
                shown = await asyncio.gather(
                    *(_show(client, root, headers, limit, model.id) for model in models)
                )
                models = [
                    model.model_copy(update=show or {})
                    for model, show in zip(models, shown, strict=True)
                ]
        models.sort(key=lambda model: model.id)
        # A list missing what a model failed to say is served, but asked for again next time.
        if None not in shown:
            _cache[name] = (time.monotonic(), models)
        return models


def _entry(name: str, entry: dict[str, Any]) -> ModelInfo | None:
    """One `/models` entry, or None without a usable id. A field of an odd shape is dropped."""
    if not isinstance(entry.get("id"), str):
        return None
    return ModelInfo(
        id=entry["id"],
        ref=f"{name}/{entry['id']}",
        context_length=_length(entry.get("context_length")),
    )


def _length(value: Any) -> int | None:
    # bool is an int to Python.
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


async def _is_ollama(client: httpx.AsyncClient, root: str, headers: dict[str, str]) -> bool:
    """Whether `root` answers as Ollama does, asked once rather than once for each model."""
    try:
        response = await client.get(f"{root}/api/version", headers=headers)
        return response.is_success and isinstance(response.json().get("version"), str)
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


async def _show(
    client: httpx.AsyncClient,
    root: str,
    headers: dict[str, str],
    limit: asyncio.Semaphore,
    model: str,
) -> dict[str, Any] | None:
    """What Ollama says of `model`, or None when it could not be asked."""
    try:
        async with limit:
            response = await client.post(f"{root}/api/show", json={"model": model}, headers=headers)
        response.raise_for_status()
        shown = response.json()
        if not isinstance(shown, dict):
            raise ValueError("not an object")
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("cannot ask Ollama at %s about %s: %s", root, model, exc)
        return None
    found: dict[str, Any] = {}
    capabilities = shown.get("capabilities")
    if isinstance(capabilities, list) and all(isinstance(c, str) for c in capabilities):
        found["capabilities"] = capabilities
    info = shown.get("model_info")
    if isinstance(info, dict) and isinstance(info.get("general.architecture"), str):
        length = _length(info.get(f"{info['general.architecture']}.context_length"))
        if length is not None:
            found["context_length"] = length
    return found
