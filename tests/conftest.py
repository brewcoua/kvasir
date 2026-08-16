import os
import tempfile

# knowledge_storm.encoder opens a litellm disk cache under Path.home() while being imported, so a
# test run would otherwise litter the developer's home with ~/.storm_local_cache. This has to
# happen before any test module imports knowledge_storm, which conftest collection guarantees. The
# image sets HOME for the same reason, since that write fails under a read-only root filesystem.
os.environ["HOME"] = tempfile.mkdtemp(prefix="kvasir-tests-home-")

# Encoder.__init__ raises without this, and constructing a Co-STORM runner constructs an Encoder.
# The service sets it in apply_environment and the image sets it as an ENV, so a test that builds a
# runner directly needs the same. Set here rather than per test, so no test depends on another
# having run apply_environment first.
os.environ.setdefault("ENCODER_API_TYPE", "openai")
