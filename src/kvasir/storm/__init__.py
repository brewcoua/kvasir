"""A fork of Stanford OVAL's STORM. See NOTICE.

No submodule is imported here: importing this package must not pull in the whole tree. Import the
module you need by its full path, as the modules here do.

What this file does is set two environment defaults, because both are read by dspy while it is being
imported, and this is the only code guaranteed to run before it. Neither writes anything; they
decide where somebody else would have.
"""

import os
import tempfile

# dspy 2.4.9 builds a joblib cache at `DSP_CACHEDIR` or, failing that, `~/cachedir_joblib`, and
# creates the directory while `dspy` is being imported. The directory is created whether or not
# caching is on, so pointing it somewhere writable is not optional. Nothing in this fork reads that
# cache: responses are cached by the gateway, not here.
os.environ.setdefault("DSP_CACHEBOOL", "False")
os.environ.setdefault("DSP_CACHEDIR", os.path.join(tempfile.gettempdir(), "kvasir-dspy-cache"))
