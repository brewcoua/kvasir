"""The HTTP service. JSON in, SSE out, no authentication.

The service is reached only over a private network and sits behind the consumer's own policy layer.
Do not add an auth layer here, and do not bind to a public interface by default.
"""
