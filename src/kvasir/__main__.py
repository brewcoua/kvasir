"""The container entrypoint: `python -m kvasir`.

Logging is configured before uvicorn starts, and uvicorn is told to configure none of its own.
Started as `uvicorn kvasir.main:app`, uvicorn applies its default dictConfig first, which pins
`propagate = False` and its own handler on `uvicorn.access` — so those lines escaped the root
handler installed later in the lifespan and printed as plain text next to everything else's JSON.
With `log_config=None` the uvicorn loggers keep propagating and land on that handler like any other.

The lifespan still calls `logs.configure`, so the app is self-sufficient under a test client.
Reading the settings twice costs nothing: `Settings.from_env` is pure.
"""

from __future__ import annotations

import uvicorn

from kvasir import logs
from kvasir.config import Settings

# Binds inside the container only. Nothing here is exposed to a network by itself.
HOST = "0.0.0.0"
PORT = 8080


def main() -> None:
    logs.configure(Settings.from_env())
    uvicorn.run("kvasir.main:app", host=HOST, port=PORT, log_config=None)


if __name__ == "__main__":
    main()
