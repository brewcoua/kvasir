"""Embeddings, through dspy.

Upstream had two unrelated embedding paths, one hardcoded to `text-embedding-3-small` behind an
`ENCODER_API_TYPE` environment variable, the other a local sentence-transformer. Both are one
`dspy.Embedder` now, pointed at the gateway, so nothing here speaks HTTP.

What is left around it is what dspy does not do: a count check, because upstream returned fewer
vectors than it was given texts, and the token accounting a run is read by.
"""

import threading
from typing import Any, List, Optional, Union

import dspy
import numpy as np

from . import runtime


class EmbeddingError(RuntimeError):
    """An embedding request failed, or answered with the wrong number of vectors.

    Upstream logged per-text failures and carried on, returning fewer vectors than it was given
    texts. Callers index the result positionally, so a short array silently attributes the wrong
    text to the wrong vector.
    """


class Encoder:
    """Embeddings from an OpenAI-compatible endpoint.

    The model name routes the way a language model's does: dspy consumes the leading `/`-separated
    segment, so the name carries an `openai/` prefix the gateway never sees.
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ):
        self.embedding_model_name = model
        # The gateway caches, so caching here would be a second place to invalidate.
        self.embedder = dspy.Embedder(model, caching=False, api_key=api_key, api_base=api_base)
        _register_usage_callback()

    def encode(self, texts: Union[str, List[str]], max_workers: int = 5) -> np.ndarray:
        """Embed one text into a 1-D array, or a list of texts into a 2-D array, row per text.

        `max_workers` is accepted and ignored. Upstream issued one request per text across a thread
        pool; dspy batches a list instead. The argument stays so existing call sites keep working.
        """
        if isinstance(texts, str):
            return self._embed([texts])[0]
        if not texts:
            return np.empty((0, 0))
        return self._embed(texts)

    def _embed(self, texts: List[str]) -> np.ndarray:
        try:
            # The sink is read here, in the caller's context, because the callback that reports
            # usage runs on litellm's logging thread and would not find it there.
            embeddings = self.embedder(texts, metadata={_SINK_KEY: runtime.current_usage_sink()})
        except Exception as exc:
            raise EmbeddingError(
                f"embedding {len(texts)} text(s) with {self.embedding_model_name} failed: {exc}"
            ) from exc

        # dspy hands back the provider's rows in the order they arrived and counts nothing, so a
        # short response would otherwise reach a caller that indexes it positionally.
        if len(embeddings) != len(texts):
            raise EmbeddingError(
                f"{self.embedding_model_name} returned {len(embeddings)} embeddings "
                f"for {len(texts)} text(s)"
            )

        return embeddings


_SINK_KEY = "kvasir_usage_sink"
_callback_lock = threading.Lock()
_callback: Any = None


def _register_usage_callback() -> None:
    """Register the embedding usage hook once, on the first `Encoder`.

    `dspy.Embedder` returns vectors and nothing else, so what a call spent is visible only to
    litellm's success hook, and litellm dispatches to `CustomLogger` subclasses alone. The hook runs
    on litellm's logging thread, after the call has returned, which is why the sink travels with the
    request rather than being looked up here.

    Registered on construction rather than at import, because importing this module must not reach
    into litellm's global state, and a process that never embeds never needs the hook.
    """
    global _callback
    with _callback_lock:
        if _callback is not None:
            return

        import litellm
        from litellm.integrations.custom_logger import CustomLogger

        class _EmbeddingUsage(CustomLogger):  # type: ignore[misc]
            def log_success_event(
                self, kwargs: dict, response_obj: Any, start_time: Any, end_time: Any
            ) -> None:
                if "embedding" not in str(kwargs.get("call_type", "")):
                    return
                metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
                sink = metadata.get(_SINK_KEY)
                if sink is None:
                    return
                usage = getattr(response_obj, "usage", None)
                tokens = int(dict(usage).get("total_tokens", 0)) if usage else 0
                # The routed name, without the provider prefix dspy consumed.
                sink.record_embedding(str(kwargs.get("model", "")), tokens)

        _callback = _EmbeddingUsage()
        litellm.callbacks = [*litellm.callbacks, _callback]


def cosine_similarity(queries: np.ndarray, corpus: np.ndarray) -> np.ndarray:
    """Row-wise cosine similarity between two 2-D arrays, shaped (len(queries), len(corpus)).

    Replaces `sklearn.metrics.pairwise.cosine_similarity`, whose only use here was this, and which
    dragged scikit-learn and scipy into the image for it. Zero vectors score 0 rather than dividing
    by zero, which is what sklearn does too.
    """
    queries = np.atleast_2d(np.asarray(queries, dtype=float))
    corpus = np.atleast_2d(np.asarray(corpus, dtype=float))
    return _normalise(queries) @ _normalise(corpus).T


def _normalise(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.where(norms == 0, 1, norms)
