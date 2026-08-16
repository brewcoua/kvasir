"""Importing the fork must not touch the filesystem or configure logging.

Upstream opened a litellm disk cache under `Path.home()` while being imported, which is why the
image used to point HOME at /tmp. These run in a subprocess because the assertions are about what
happens during import, and the test process has already imported everything.
"""

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kvasir.storm.runtime import configure_cache, litellm


def _run(source: str, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        env={"PATH": "/usr/bin:/bin", "HOME": str(home)},
        capture_output=True,
        text=True,
    )


def test_import_opens_no_cache(tmp_path: Path) -> None:
    result = _run(
        """
        import kvasir.storm.lm
        import kvasir.storm.encoder
        from kvasir.storm.runtime import litellm

        assert litellm.cache is None, litellm.cache
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr


def test_import_writes_nothing_under_home(tmp_path: Path) -> None:
    """A read-only home must not stop the fork being imported.

    This covers dspy's joblib cache as much as litellm's: dspy 2.4.9 creates `~/cachedir_joblib`
    while being imported unless `DSP_CACHEDIR` says otherwise, which `kvasir.storm.__init__` does.
    """
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)
    try:
        result = _run(
            "import kvasir.storm.lm, kvasir.storm.encoder, kvasir.storm.storm_wiki.engine", home
        )
    finally:
        home.chmod(0o700)

    assert result.returncode == 0, result.stderr
    assert list(home.iterdir()) == []


def test_import_does_not_configure_the_root_logger(tmp_path: Path) -> None:
    result = _run(
        """
        import logging

        import kvasir.storm.interface
        import kvasir.storm.utils

        assert logging.getLogger().handlers == [], logging.getLogger().handlers
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr


def test_importing_the_package_pulls_in_no_submodule(tmp_path: Path) -> None:
    result = _run(
        """
        import sys

        import kvasir.storm

        pulled = [name for name in sys.modules if name.startswith("kvasir.storm.")]
        assert pulled == [], pulled
        """,
        tmp_path,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(autouse=True)
def _restore_cache() -> object:
    before = litellm.cache
    yield
    litellm.cache = before


def test_configure_cache_creates_the_directory(tmp_path: Path) -> None:
    cache_dir = tmp_path / "does" / "not" / "exist"
    configure_cache(cache_dir)

    assert cache_dir.is_dir()
    assert litellm.cache is not None


def test_configure_cache_with_none_disables_caching(tmp_path: Path) -> None:
    configure_cache(tmp_path)
    configure_cache(None)

    assert litellm.cache is None
