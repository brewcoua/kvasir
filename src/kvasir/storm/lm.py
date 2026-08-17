"""The only language model wrapper in the fork: a `dspy.LM` that reports what a run spent.

Upstream carried a copy of dspy's `LM` class, a text-completion path, an in-process LRU cache and
several wrappers for providers deprecated after v1.1.0. Nothing here called any of it: every model
is built by `kvasir.runners` as a chat model pointed at one gateway.

What is left is the accounting dspy does not do. dspy tracks usage per call and litellm prices what
it recognises, but neither knows which STORM stage is spending, and a self-hosted gateway prices its
own models. `forward` is the one seam where the response, the role and the run's usage sink are all
in scope.
"""

import logging
import threading
from typing import Any

import backoff
import dspy

from . import runtime

logger = logging.getLogger(__name__)

# What a later attempt can plausibly fix. A bad request, an auth failure or a context-length
# overflow is deterministic, so it fails immediately rather than after sixteen minutes of backoff —
# which is what litellm's own `num_retries` does, since it retries a 400 as readily as a 429.
RETRYABLE = (
    dspy.LMRateLimitError,
    dspy.LMServerError,
    dspy.LMTimeoutError,
    dspy.LMTransportError,
)


def _log_retry(details: dict) -> None:
    logger.warning(
        "gateway call failed, retrying in %.1fs (attempt %d)",
        details["wait"],
        details["tries"],
    )


class GatewayModel(dspy.LM):
    """A chat model on an OpenAI-compatible gateway, tagged with the role it serves.

    `api_key` and `api_base` are passed through to `dspy.LM`, which keeps them in `kwargs`. That is
    also what `LMConfigs.to_dict` and `LMConfigs.log` serialise, and both strip keys beginning with
    `api_` to keep credentials out of session files and run configuration.
    """

    def __init__(self, model: str, role: str | None = None, **kwargs: Any):
        # The gateway caches responses, and dspy's on-disk cache would be a second place to
        # invalidate. Retrying is `forward`'s, which is selective about what it retries.
        super().__init__(model=model, cache=False, num_retries=0, **kwargs)
        self.role = role
        self._token_usage_lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def get_usage_and_reset(self) -> dict[str, dict[str, int]]:
        """Get the total tokens used and reset the token usage."""
        usage = {
            self.model: {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }
        }
        self.prompt_tokens = 0
        self.completion_tokens = 0

        return usage

    @backoff.on_exception(backoff.expo, RETRYABLE, max_time=1000, on_backoff=_log_retry)
    def forward(self, prompt: str | None = None, messages: Any = None, **kwargs: Any) -> Any:
        response = super().forward(prompt=prompt, messages=messages, **kwargs)

        usage = getattr(response, "usage", None) or {}
        if not isinstance(usage, dict):
            usage = dict(usage)
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        with self._token_usage_lock:
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens

        runtime.record_lm_usage(
            self.model,
            self.role,
            prompt_tokens,
            completion_tokens,
            response_cost(response),
        )
        return response


def response_cost(response: Any) -> float:
    """What the call cost, or 0.0 when nothing reports one.

    litellm prices the models it has in its map, which a self-hosted gateway's names are not in, so
    three sources are tried. A LiteLLM proxy returns a header, which litellm passes through; Bifrost
    puts a cost in the usage object, either as a number or split into prompt and completion. A
    gateway that reports nothing leaves the run's cost at zero, which is not the same as free.
    """
    hidden = getattr(response, "_hidden_params", None) or {}

    cost = _number(hidden.get("response_cost"))
    if cost:
        return cost

    headers = hidden.get("additional_headers") or {}
    for key, value in headers.items():
        if key.endswith("x-litellm-response-cost"):
            try:
                return float(value)
            except (TypeError, ValueError):
                break

    usage = getattr(response, "usage", None) or {}
    reported = usage.get("cost") if isinstance(usage, dict) else getattr(usage, "cost", None)
    if isinstance(reported, dict):
        return _number(reported.get("prompt_cost")) + _number(reported.get("completion_cost"))
    return _number(reported)


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
