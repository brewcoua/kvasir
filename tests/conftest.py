import os
import tempfile

# knowledge_storm.encoder opens a litellm disk cache under Path.home() while being imported, so a
# test run would otherwise litter the developer's home with ~/.storm_local_cache. This has to
# happen before any test module imports knowledge_storm, which conftest collection guarantees. The
# image sets HOME for the same reason, since that write fails under a read-only root filesystem.
os.environ["HOME"] = tempfile.mkdtemp(prefix="kvasir-tests-home-")
