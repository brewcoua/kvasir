"""The single place this package configures litellm.

Upstream did this twice, in `lm.py` and `encoder.py`, as a side effect of importing either module.
Two things went wrong with that. The disk cache was opened under `Path.home()`, so importing the
package wrote to the filesystem and failed outright under a read-only root; and there was no way to
choose a different directory, or none at all, without editing the source.

Importing this module still sets the process-wide litellm flags below, because they are policy for
this fork rather than deployment configuration, and none of them touch the filesystem or the
network. The cache is the part that does, so it is opened only by an explicit `configure_cache`
call.
"""

import os

import litellm
from litellm.caching.caching import Cache

# Gateways route to models that reject parameters other models accept, and a rejected parameter
# should not fail a run.
litellm.drop_params = True
litellm.telemetry = False


def configure_cache(cache_dir: str | os.PathLike[str] | None) -> None:
    """Open a litellm disk cache under `cache_dir`, or disable caching when it is None.

    Idempotent. Not called on import, so a caller that never calls it runs uncached rather than
    writing somewhere it did not choose.
    """
    if cache_dir is None:
        litellm.cache = None
        return
    os.makedirs(cache_dir, exist_ok=True)
    litellm.cache = Cache(disk_cache_dir=str(cache_dir), type="disk")


__all__ = ["Cache", "configure_cache", "litellm"]
