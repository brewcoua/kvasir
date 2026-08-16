import os

# Encoder.__init__ raises without this, and constructing a Co-STORM runner constructs an Encoder.
# The service sets it in apply_environment and the image sets it as an ENV, so a test that builds a
# runner directly needs the same. Set here rather than per test, so no test depends on another
# having run apply_environment first.
os.environ.setdefault("ENCODER_API_TYPE", "openai")
