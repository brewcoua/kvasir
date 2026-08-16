"""The only language model wrapper in the fork: chat completions against the gateway.

Upstream carried a copy of dspy's `LM` class, a text-completion path, an in-process LRU cache and
several wrappers for providers deprecated after v1.1.0. Nothing here called any of it: every model
is built by `kvasir.runners` as a chat model pointed at one gateway.

The surface below is what dspy 2.4.9 reads off a language model — `__call__` returning a list of
strings, `kwargs`, and `history` — so this stays a drop-in for `dspy.settings.context(lm=...)`.
"""

import logging
import threading

import backoff
import httpx

from . import runtime
from .gateway import GatewayError, RetryableGatewayError, post, response_cost

logger = logging.getLogger(__name__)


def _log_retry(details: dict) -> None:
    logger.warning(
        "gateway call failed, retrying in %.1fs (attempt %d)",
        details["wait"],
        details["tries"],
    )


class GatewayModel:
    """A chat model on an OpenAI-compatible gateway.

    `api_key` and `api_base` are kept in `kwargs` rather than as attributes because
    `LMConfigs.to_dict` and `LMConfigs.log` serialise that dict, and both strip keys beginning with
    `api_` to keep credentials out of session files and run configuration.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1000,
        **kwargs,
    ):
        self.model = model
        self.kwargs = dict(
            temperature=temperature,
            max_tokens=max_tokens,
            api_key=api_key,
            api_base=api_base,
            **kwargs,
        )
        self.history = []
        self._token_usage_lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def log_usage(self, response):
        usage_data = response.get("usage")
        if usage_data:
            with self._token_usage_lock:
                self.prompt_tokens += usage_data.get("prompt_tokens", 0)
                self.completion_tokens += usage_data.get("completion_tokens", 0)

    def get_usage_and_reset(self):
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

    # Upstream left the only live wrapper as the only one without a retry: every other @backoff in
    # this file was on a class deprecated after v1.1.0. A rate limit or a dropped connection
    # therefore failed the whole stage on the first try.
    @backoff.on_exception(
        backoff.expo,
        (httpx.TransportError, RetryableGatewayError),
        max_time=1000,
        on_backoff=_log_retry,
    )
    def _request(self, payload: dict) -> tuple[httpx.Response, dict]:
        response = post("/chat/completions", self._api_base, self.kwargs.get("api_key"), payload)
        return response, response.json()

    @property
    def _api_base(self) -> str:
        api_base = self.kwargs.get("api_base")
        if not api_base:
            raise GatewayError(f"no api_base configured for {self.model}")
        return api_base

    def __call__(self, prompt=None, messages=None, **kwargs):
        messages = messages or [{"role": "user", "content": prompt}]
        kwargs = {**self.kwargs, **kwargs}
        # Transport, not generation parameters.
        params = {key: value for key, value in kwargs.items() if not key.startswith("api_")}

        response, body = self._request(dict(model=self.model, messages=messages, **params))

        self.log_usage(body)
        usage = body.get("usage") or {}
        cost = response_cost(response, body)
        runtime.record_lm_usage(
            self.model,
            # Set by whatever built this model, so a run can be read as which stage spent what.
            getattr(self, "role", None),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            cost,
        )
        outputs = [
            choice["message"]["content"] if "message" in choice else choice["text"]
            for choice in body["choices"]
        ]

        self.history.append(
            dict(
                prompt=prompt,
                messages=messages,
                kwargs=params,
                response=body,
                outputs=outputs,
                usage=dict(usage),
                cost=cost,
            )
        )

        return outputs
