"""The stored settings: what each is, its value, and changing it.

A change is checked against every other stored setting before anything is written, then applies to
the running service at once, except the few read only at startup, which say so with `restart`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import Field
from typing import Annotated, Any

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from kvasir.api.streaming import RunSlots
from kvasir.config import MODEL_KEYS, STORED_FIELDS, ConfigError, Settings
from kvasir.research import fetch
from kvasir.research.favicons import Favicons, FaviconStore
from kvasir.research.pages import PageStore
from kvasir.runners import build_sources
from kvasir.settings_store import SettingsStore
from kvasir.storm.runtime import configure_context, configure_fetch

router = APIRouter()

# One change at a time, so two cannot each pass against the rows the other is replacing.
_lock = threading.Lock()

_TYPES = {
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "str": "text",
    "str | None": "text",
    "tuple[str, ...]": "list",
}


class SettingInfo(BaseModel):
    key: str
    # `integer`, `number`, `boolean`, `text`, `list` of text, or `model`, a `<provider>/<name>`.
    type: str
    help: str
    value: Any
    default: Any
    # Whether `value` comes from kvasir.db rather than being the default.
    stored: bool
    # The service runs nothing while a required setting is empty.
    required: bool
    # Whether `value` may be null, which `""` stores.
    nullable: bool
    # Read at startup only, so a change waits for a restart.
    restart: bool
    minimum: int | None = None
    choices: list[str] | None = None


class SettingsView(BaseModel):
    fields: list[SettingInfo]
    # The required settings still empty.
    missing: list[str]
    # Settings changed since startup that apply only at the next.
    restart_required: list[str]


def _info(settings: Settings, stored: set[str], key: str, f: Field[Any]) -> SettingInfo:
    return SettingInfo(
        key=key,
        type="model" if key in MODEL_KEYS else _TYPES[str(f.type)],
        help=f.metadata["help"],
        value=settings.value(key),
        default=f.default,
        stored=key in stored,
        required=f.metadata.get("required", False),
        nullable=f.type == "str | None",
        restart=f.metadata.get("restart", False),
        minimum=f.metadata.get("min", 1) if f.type == "int" else (0 if f.type == "float" else None),
        choices=list(f.metadata["choices"]) if "choices" in f.metadata else None,
    )


def _view(app: FastAPI) -> SettingsView:
    settings: Settings = app.state.settings
    started: Settings = app.state.started
    stored = set(SettingsStore(settings.db).load())
    return SettingsView(
        fields=[_info(settings, stored, key, f) for key, f in STORED_FIELDS],
        missing=list(settings.missing),
        restart_required=[
            key
            for key, f in STORED_FIELDS
            if f.metadata.get("restart") and settings.value(key) != started.value(key)
        ],
    )


@router.get("/v1/settings")
def get_settings(http: Request) -> SettingsView:
    """Every stored setting, with its value, its default and what it accepts."""
    return _view(http.app)


@router.patch("/v1/settings", response_model=SettingsView)
def patch_settings(
    http: Request, changes: Annotated[Any, Body()] = None
) -> SettingsView | JSONResponse:
    """Set each key to its value, or back to its default with null. Nothing is written unless every
    stored setting is valid afterwards; 422 names the first that is not."""
    if not isinstance(changes, dict):
        return JSONResponse({"message": "send an object of values by key"}, status_code=422)
    with _lock:
        settings: Settings = http.app.state.settings
        store = SettingsStore(settings.db)
        stored: dict[str, Any] = {}
        for key, text in store.load().items():
            if key in changes:
                continue
            try:
                stored[key] = json.loads(text)
            except ValueError:
                return JSONResponse(
                    {"message": f"{key} is not JSON; reset it with null"}, status_code=422
                )
        stored |= {key: value for key, value in changes.items() if value is not None}
        try:
            changed = settings.with_stored(stored, strict=True)
        except ConfigError as exc:
            return JSONResponse({"message": str(exc)}, status_code=422)
        apply(http.app, changed, settings)
        try:
            # As parsed, so the row says what GET shows. A null text field is stored as "", since
            # no row would mean its default instead.
            store.write(
                {
                    key: None if value is None else _or_empty(changed.value(key))
                    for key, value in changes.items()
                }
            )
        except sqlite3.Error:
            apply(http.app, settings, changed)
            raise
        return _view(http.app)


def _or_empty(value: Any) -> Any:
    return "" if value is None else value


@router.delete("/v1/settings/{key}", response_model=SettingsView)
def reset_setting(http: Request, key: str) -> SettingsView | JSONResponse:
    """Set `key` back to its default. Also removes a key this version does not know."""
    return patch_settings(http, {key: None})


def apply(app: FastAPI, settings: Settings, previous: Settings | None = None) -> None:
    """Make `settings` the running ones; None for `previous` is the start. What can fail is built
    before anything changes, so a failure leaves the running settings whole."""
    if previous is not None and settings.favicons == previous.favicons:
        favicons = app.state.favicons
    else:
        # The database file is not made when favicons are off.
        favicons = (
            Favicons(settings.favicons, FaviconStore(settings.favicons_db))
            if settings.favicons.providers
            else None
        )
    # The runs' own instance, so a mark takes effect for a run in flight. None when off. Cached by
    # its settings, so this rebuilds only on a change.
    sources = build_sources(settings)
    pages = PageStore(settings.pages_db) if previous is None else fetch.cache()

    app.state.settings = settings
    app.state.favicons = favicons
    app.state.sources = sources
    configure_context(settings.model.context_tokens)
    configure_fetch(
        settings.fetch.contact,
        settings.fetch.host_interval_seconds,
        settings.fetch.render,
        settings.fetch.stealth,
    )
    fetch.configure_cache(pages, settings.fetch.max_age_hours)
    if previous is None:
        # A run holds a thread for minutes, so saturation is rejected rather than queued.
        app.state.run_slots = RunSlots(settings.runs.max_concurrent)
    else:
        app.state.run_slots.limit = settings.runs.max_concurrent
