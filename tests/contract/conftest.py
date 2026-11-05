"""One configured service for the whole suite, and a throwaway one for a test that needs its own."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from harness import Fakes, Service


@pytest.fixture(scope="session")
def fakes() -> Iterator[Fakes]:
    instance = Fakes()
    yield instance
    instance.close()


@pytest.fixture(scope="session")
def service(fakes: Fakes, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Service]:
    """Set up through the API and restarted, as a deployment is: some settings need a restart."""
    instance = Service(tmp_path_factory.mktemp("contract") / "data", fakes.environment())
    instance.start()
    try:
        with instance.client() as client:
            assert client.patch("/v1/settings", json=fakes.settings()).status_code == 200
        instance.restart()
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def client(service: Service) -> Iterator[httpx.Client]:
    with service.client() as instance:
        yield instance


@pytest.fixture
def new_service(fakes: Fakes, tmp_path: Path) -> Iterator[Callable[[], Service]]:
    """A service of the test's own, with an empty data directory."""
    started: list[Service] = []

    def start() -> Service:
        instance = Service(tmp_path / f"data{len(started)}", fakes.environment())
        started.append(instance)
        instance.start()
        return instance

    yield start
    for instance in started:
        instance.stop()
