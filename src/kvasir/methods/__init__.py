"""The research methods. Each orchestrates the shared steps in `kvasir.research` its own way.

A method is an entry of `METHODS` and a member of `RunRequest` in `kvasir.schemas.requests`.
"""

from __future__ import annotations

from typing import Any

from kvasir.methods.base import Method
from kvasir.methods.ledger.method import LEDGER
from kvasir.methods.storm.method import STORM

METHODS: dict[str, Method[Any]] = {method.name: method for method in (LEDGER, STORM)}
