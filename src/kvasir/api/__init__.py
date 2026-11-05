"""The HTTP service. JSON in, SSE out, behind one shared token (`kvasir.api.auth`).

Do not bind to a public interface by default: the token guards the routes, and nothing here
limits how often it may be guessed.
"""
