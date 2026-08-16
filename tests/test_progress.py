import asyncio
import json

import pytest

from kvasir.models import Error, Progress
from kvasir.progress import OUTLINE, RESEARCH, ProgressStream, StormProgressHandler
from kvasir.sse import frame


async def drain(stream):
    return [event async for event in stream]


@pytest.mark.asyncio
async def test_events_arrive_in_order_and_iteration_ends_on_close():
    stream = ProgressStream()
    stream.publish(RESEARCH, "first")
    stream.publish(OUTLINE, "second")
    stream.close()

    events = await drain(stream)

    assert [(event.stage, event.detail) for event in events] == [
        (RESEARCH, "first"),
        (OUTLINE, "second"),
    ]


@pytest.mark.asyncio
async def test_publishing_from_a_worker_thread_reaches_the_event_loop():
    stream = ProgressStream()

    def worker():
        for index in range(3):
            stream.publish(RESEARCH, f"turn {index}")
        stream.close()

    consumer = asyncio.create_task(drain(stream))
    await asyncio.to_thread(worker)
    events = await consumer

    assert [event.detail for event in events] == ["turn 0", "turn 1", "turn 2"]


@pytest.mark.asyncio
async def test_handler_maps_callbacks_to_stages():
    stream = ProgressStream()
    handler = StormProgressHandler(stream)

    handler.on_identify_perspective_start()
    handler.on_identify_perspective_end(perspectives=["a", "b"])
    handler.on_information_gathering_start()
    handler.on_dialogue_turn_end(dlg_turn=object())
    handler.on_dialogue_turn_end(dlg_turn=object())
    handler.on_information_gathering_end()
    handler.on_information_organization_start()
    handler.on_direct_outline_generation_end(outline="# a")
    handler.on_outline_refinement_end(outline="# a")
    stream.close()

    events = await drain(stream)

    assert [event.stage for event in events] == [RESEARCH] * 6 + [OUTLINE] * 3
    assert "identified 2 perspectives" in [event.detail for event in events]
    assert "completed conversation turn 2" in [event.detail for event in events]


def test_frame_is_terminated_by_a_blank_line():
    text = frame("progress", Progress(stage=RESEARCH, detail="asking"))

    assert text.endswith("\n\n")
    assert text.splitlines()[0] == "event: progress"


def test_frame_payload_is_json():
    text = frame("progress", Progress(stage=RESEARCH, detail="asking"))
    data = text.splitlines()[1].removeprefix("data: ")

    assert json.loads(data) == {"stage": RESEARCH, "detail": "asking"}


def test_newlines_in_a_payload_cannot_end_the_frame_early():
    text = frame("error", Error(message="line one\nline two"))

    # Two lines plus the blank terminator. A raw newline in the payload would make it more.
    assert len(text.split("\n")) == 4
    assert json.loads(text.splitlines()[1].removeprefix("data: "))["message"] == (
        "line one\nline two"
    )
