"""Co-STORM operations. Each one loads a session, does one thing, and saves it back.

Every call here is blocking and slow, so all of them run in a worker thread. Nothing holds a runner
between requests: a session lives on disk, which is what lets it survive a restart.
"""

from __future__ import annotations

from typing import Any

from kvasir.config import Settings
from kvasir.models import Citation, Report, SessionInfo, SessionRequest, Turn
from kvasir.progress import ARTICLE, CoStormProgressHandler, ProgressStream
from kvasir.runners import build_costorm_runner, load_costorm_runner
from kvasir.sessions import SessionStore


def create(
    settings: Settings, store: SessionStore, request: SessionRequest, stream: ProgressStream
) -> SessionInfo:
    """Build a session and warm start it. Slow: this is a miniature STORM run."""
    handler = CoStormProgressHandler(stream)
    runner = build_costorm_runner(
        settings,
        request.topic,
        model_fast=request.model_fast,
        model_strong=request.model_strong,
    )
    runner.callback_handler = handler
    runner.discourse_manager.callback_handler = handler

    runner.warm_start()
    store.save(request.session_id, runner.to_dict())
    return _info(request.session_id, store, runner.to_dict())


def step(
    settings: Settings, store: SessionStore, session_id: str, utterance: str, stream: ProgressStream
) -> Turn:
    """Advance the conversation by one turn.

    With an utterance the user speaks and the turn is recorded. Without one an agent takes a turn.
    """
    handler = CoStormProgressHandler(stream)
    state = store.load(session_id)
    runner = load_costorm_runner(settings, state)
    runner.callback_handler = handler
    runner.discourse_manager.callback_handler = handler

    turn = runner.step(user_utterance=utterance)
    store.save(session_id, runner.to_dict())

    return Turn(
        role=turn.role,
        role_description=turn.role_description,
        utterance=turn.utterance,
        utterance_type=turn.utterance_type,
        citations=_citations(turn.cited_info),
        mind_map_reorganised=handler.mind_map_reorganised,
    )


def report(
    settings: Settings, store: SessionStore, session_id: str, stream: ProgressStream
) -> Report:
    """Generate the report. Reading only, so the session is not written back.

    Upstream's `generate_report` invokes no callback, so the two slow parts are announced here
    rather than through a handler: rebuilding the knowledge base, then writing the report.
    """
    stream.publish(ARTICLE, "loading the round table")
    runner = load_costorm_runner(settings, store.load(session_id))
    stream.publish(ARTICLE, "writing the report")
    return Report(
        report=runner.generate_report(),
        citations=_citations(runner.knowledge_base.info_uuid_to_info_dict),
    )


def info(store: SessionStore, session_id: str) -> SessionInfo:
    return _info(session_id, store, store.load(session_id))


def _info(session_id: str, store: SessionStore, state: dict[str, Any]) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        topic=state["runner_argument"]["topic"],
        turn_count=len(state["conversation_history"]),
        experts=[expert["role_name"] for expert in state["experts"]],
        updated_at=store.updated_at(session_id),
    )


def _citations(cited: dict[int, Any]) -> list[Citation]:
    """Sources keyed by the number the `[n]` markers use."""
    citations = [
        Citation(
            index=index,
            url=getattr(information, "url", ""),
            title=getattr(information, "title", ""),
            snippet=next(iter(getattr(information, "snippets", []) or []), ""),
        )
        for index, information in cited.items()
    ]
    citations.sort(key=lambda citation: citation.index)
    return citations
