"""Keeps a developer's own configuration file out of the tests.

`Settings.from_env()` reads `kvasir.toml` from the working directory, which is the repository root
under pytest, and the quickstart puts one there. Tests that start the app would otherwise run
against it.
"""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _no_config_file(monkeypatch, tmp_path):
    monkeypatch.delenv("KVASIR_CONFIG", raising=False)
    monkeypatch.setattr("kvasir.config.DEFAULT_CONFIG_FILE", tmp_path / "absent.toml")


@pytest.fixture
def default_config_file(monkeypatch):
    """Restores the real default, for the one test about finding it."""
    monkeypatch.setattr("kvasir.config.DEFAULT_CONFIG_FILE", Path("kvasir.toml"))
