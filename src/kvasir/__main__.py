"""The container entrypoint: `python -m kvasir`.

Logging is configured before uvicorn starts, and uvicorn is told to configure none of its own.
Started as `uvicorn kvasir.api.app:app`, uvicorn applies its default dictConfig first, which pins
`propagate = False` and its own handler on `uvicorn.access` — so those lines escaped the root
handler installed later in the lifespan and printed as plain text next to everything else's JSON.
With `log_config=None` the uvicorn loggers keep propagating and land on that handler like any other.

The lifespan still calls `logs.configure`, so the app is self-sufficient under a test client.
Only the environment is read here, which holds the logging and the address; the stored settings
are the lifespan's.
"""

from __future__ import annotations

import uvicorn

from kvasir import logs
from kvasir.config import Settings


def main() -> None:
    settings = Settings.load(stored={})
    logs.configure(settings)
    uvicorn.run("kvasir.api.app:app", host=settings.host, port=settings.port, log_config=None)


if __name__ == "__main__":
    main()
