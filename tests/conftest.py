"""Keeps a developer's own configuration file, and the fetch politeness, out of the tests.

`Settings.from_env()` reads `kvasir.toml` from the working directory, which is the repository root
under pytest, and the quickstart puts one there. Tests that start the app would otherwise run
against it.
"""

from pathlib import Path

import pytest

from kvasir.research import fetch
from kvasir.storm import runtime


@pytest.fixture(autouse=True)
def _no_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("KVASIR_CONFIG", raising=False)
    monkeypatch.setattr("kvasir.config.DEFAULT_CONFIG_FILE", tmp_path / "absent.toml")


@pytest.fixture
def default_config_file(monkeypatch):
    """Restores the real default, for the one test about finding it."""
    monkeypatch.setattr("kvasir.config.DEFAULT_CONFIG_FILE", Path("kvasir.toml"))


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
