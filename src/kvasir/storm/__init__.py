"""A fork of Stanford OVAL's STORM. See NOTICE.

No submodule is imported here: importing this package must not pull in the whole tree. Import the
module you need by its full path, as the modules here do.

What this file does is set one environment default, because dspy reads it while it is being
imported, and this is the only code guaranteed to run before it. It writes nothing; it decides where
somebody else would have.
"""

import os
import tempfile

# dspy opens a disk cache at `DSPY_CACHEDIR` or, failing that, `~/.dspy_cache`, and creates the
# directory while `dspy` is being imported — before any `dspy.configure_cache` call could say
# otherwise. Nothing in this fork reads that cache: `GatewayModel` is built with `cache=False`
# because the gateway caches responses, and doing it twice only means two places to invalidate.
os.environ.setdefault("DSPY_CACHEDIR", os.path.join(tempfile.gettempdir(), "kvasir-dspy-cache"))
