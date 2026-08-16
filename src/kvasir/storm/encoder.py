import threading
from typing import List, Optional, Union

import numpy as np

from .runtime import litellm


class EmbeddingError(RuntimeError):
    """An embedding request failed.

    Upstream logged per-text failures and carried on, returning fewer vectors than it was given
    texts and, worse, misaligning the ones it did return against the input order. Callers index the
    result positionally, so a short array silently attributes the wrong text to the wrong vector.
    """


class Encoder:
    """Embeddings from an OpenAI-compatible endpoint, through litellm.

    Upstream chose between two hardcoded model names on an ENCODER_API_TYPE environment variable
    and dropped `api_base` for the openai branch, so the only way to reach a gateway was to set
    OPENAI_API_BASE and hope litellm read it. Both are arguments here.

    Check https://docs.litellm.ai/docs/embedding/supported_embedding for what a model name may be.
    """

    def __init__(
        self,
        model: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
    ):
        self.embedding_model_name = model
        self.kargs = {}
        if api_key is not None:
            self.kargs["api_key"] = api_key
        if api_base is not None:
            self.kargs["api_base"] = api_base
        self.total_token_usage = 0
        self._token_usage_lock = threading.Lock()

    def get_total_token_usage(self, reset: bool = False) -> int:
        with self._token_usage_lock:
            token_usage = self.total_token_usage
            if reset:
                self.total_token_usage = 0
        return token_usage

    def encode(self, texts: Union[str, List[str]], max_workers: int = 5) -> np.ndarray:
        """Embed one text into a 1-D array, or a list of texts into a 2-D array, row per text.

        `max_workers` is accepted and ignored. Upstream issued one request per text across a thread
        pool; a list goes in a single request now, which is both faster and what the endpoints
        expect. The argument stays so existing call sites keep working.
        """
        if isinstance(texts, str):
            return self._embed([texts])[0]
        if not texts:
            return np.empty((0, 0))
        return self._embed(texts)

    def _embed(self, texts: List[str]) -> np.ndarray:
        try:
            response = litellm.embedding(
                model=self.embedding_model_name, input=texts, caching=True, **self.kargs
            )
        except Exception as exc:
            raise EmbeddingError(
                f"embedding {len(texts)} text(s) with {self.embedding_model_name} failed: {exc}"
            ) from exc

        # litellm does not promise the response preserves input order, and a short response is
        # what upstream turned into silently misaligned vectors.
        data = sorted(response.data, key=lambda item: item["index"])
        if len(data) != len(texts):
            raise EmbeddingError(
                f"{self.embedding_model_name} returned {len(data)} embeddings "
                f"for {len(texts)} text(s)"
            )

        with self._token_usage_lock:
            self.total_token_usage += response.get("usage", {}).get("total_tokens", 0)

        return np.array([item["embedding"] for item in data])


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
