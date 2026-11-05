"""Keeps a developer's own environment, and the fetch politeness, out of the tests."""

import os

import pytest

from kvasir.research import fetch
from kvasir.storm import runtime


@pytest.fixture(autouse=True)
def _no_kvasir_environment(request, monkeypatch):
    """A test sets what it needs. A developer's KVASIR_DATA_DIR would point it at their data. An
    integration test runs against exactly that, so it keeps the environment."""
    if request.node.get_closest_marker("integration"):
        return
    for name in os.environ:
        if name.startswith("KVASIR_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_fetch_waits(monkeypatch):
    """No wait between requests to one host, which tests make by the dozen to example.org, and no
    browser unless a test asks for one: a developer with the `browser` extra would launch it."""
    monkeypatch.setattr(runtime, "_host_interval", 0.0)
    monkeypatch.setattr(runtime, "_fetch_render", "off")
    monkeypatch.setattr(fetch, "_cache", None)


@pytest.fixture(autouse=True)
def _no_robots_requests(monkeypatch):
    """Every lead allowed with no request for its robots.txt. A test of `allowed` itself imports
    it by name, which this does not replace."""
    monkeypatch.setattr(fetch, "allowed", lambda url: True)
    monkeypatch.setattr(fetch, "_robots", {})
