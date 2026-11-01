"""Server-sent event framing.

The format is one `event:` line, one `data:` line and a blank line terminator. Payloads are always
JSON, so a value can never contain the raw newline that would end a frame early.
"""

from __future__ import annotations

from pydantic import BaseModel

MEDIA_TYPE = "text/event-stream"

# Proxies that buffer a response defeat the point of streaming it.
HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def frame(event: str, payload: BaseModel) -> str:
    """Encode one event. The trailing blank line is what tells the client the frame is complete."""
    return f"event: {event}\ndata: {payload.model_dump_json()}\n\n"
