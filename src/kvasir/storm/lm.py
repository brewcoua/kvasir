import backoff
import functools
import logging
import os
import threading
from typing import Optional, Literal
import ujson


from dsp import backoff_hdlr

############################
# Code copied from https://github.com/stanfordnlp/dspy/blob/main/dspy/clients/lm.py on Sep 29, 2024

from . import runtime
from .runtime import litellm

logger = logging.getLogger(__name__)

LM_LRU_CACHE_MAX_SIZE = 3000

# What a retry can plausibly fix. A bad request, an auth failure or a context-length overflow is
# deterministic, so it fails immediately rather than after sixteen minutes of backoff.
_RETRYABLE = (
    litellm.APIConnectionError,
    litellm.InternalServerError,
    litellm.RateLimitError,
    litellm.ServiceUnavailableError,
    litellm.Timeout,
)


class LM:
    def __init__(
        self,
        model,
        model_type="chat",
        temperature=0.0,
        max_tokens=1000,
        cache=True,
        **kwargs,
    ):
        self.model = model
        self.model_type = model_type
        self.cache = cache
        self.kwargs = dict(temperature=temperature, max_tokens=max_tokens, **kwargs)
        self.history = []

        if "o1-" in model:
            assert (
                max_tokens >= 5000 and temperature == 1.0
            ), "OpenAI's o1-* models require passing temperature=1.0 and max_tokens >= 5000 to `dspy.LM(...)`"

    def __call__(self, prompt=None, messages=None, **kwargs):
        # Build the request.
        cache = kwargs.pop("cache", self.cache)
        messages = messages or [{"role": "user", "content": prompt}]
        kwargs = {**self.kwargs, **kwargs}

        # Make the request and handle LRU & disk caching.
        if self.model_type == "chat":
            completion = cached_litellm_completion if cache else litellm_completion
        else:
            completion = (
                cached_litellm_text_completion if cache else litellm_text_completion
            )

        response = completion(
            ujson.dumps(dict(model=self.model, messages=messages, **kwargs))
        )
        outputs = [
            c.message.content if hasattr(c, "message") else c["text"]
            for c in response["choices"]
        ]

        # Logging, with removed api key & where `cost` is None on cache hit.
        kwargs = {k: v for k, v in kwargs.items() if not k.startswith("api_")}
        entry = dict(prompt=prompt, messages=messages, kwargs=kwargs, response=response)
        entry = dict(**entry, outputs=outputs, usage=dict(response["usage"]))
        entry = dict(
            **entry, cost=response.get("_hidden_params", {}).get("response_cost")
        )
        self.history.append(entry)

        return outputs

    def inspect_history(self, n: int = 1):
        _inspect_history(self, n)


@functools.lru_cache(maxsize=LM_LRU_CACHE_MAX_SIZE)
def cached_litellm_completion(request):
    return litellm_completion(request, cache={"no-cache": False, "no-store": False})


def litellm_completion(request, cache={"no-cache": True, "no-store": True}):
    kwargs = ujson.loads(request)
    return litellm.completion(cache=cache, **kwargs)


@functools.lru_cache(maxsize=LM_LRU_CACHE_MAX_SIZE)
def cached_litellm_text_completion(request):
    return litellm_text_completion(
        request, cache={"no-cache": False, "no-store": False}
    )


def litellm_text_completion(request, cache={"no-cache": True, "no-store": True}):
    kwargs = ujson.loads(request)

    # Extract the provider and model from the model string.
    model = kwargs.pop("model").split("/", 1)
    provider, model = model[0] if len(model) > 1 else "openai", model[-1]

    # Use the API key and base from the kwargs, or from the environment.
    api_key = kwargs.pop("api_key", None) or os.getenv(f"{provider}_API_KEY")
    api_base = kwargs.pop("api_base", None) or os.getenv(f"{provider}_API_BASE")

    # Build the prompt from the messages.
    prompt = "\n\n".join(
        [x["content"] for x in kwargs.pop("messages")] + ["BEGIN RESPONSE:"]
    )

    return litellm.text_completion(
        cache=cache,
        model=f"text-completion-openai/{model}",
        api_key=api_key,
        api_base=api_base,
        prompt=prompt,
        **kwargs,
    )


def _green(text: str, end: str = "\n"):
    return "\x1b[32m" + str(text).lstrip() + "\x1b[0m" + end


def _red(text: str, end: str = "\n"):
    return "\x1b[31m" + str(text) + "\x1b[0m" + end


def _inspect_history(lm, n: int = 1):
    """Prints the last n prompts and their completions.

    Deliberately prints, with ANSI colour: it is a debugging entry point called by hand, never by
    the pipeline. Routing it through logging would put a coloured transcript in the service's logs.
    """

    for item in lm.history[-n:]:
        messages = item["messages"] or [{"role": "user", "content": item["prompt"]}]
        outputs = item["outputs"]

        print("\n\n\n")
        for msg in messages:
            print(_red(f"{msg['role'].capitalize()} message:"))
            print(msg["content"].strip())
            print("\n")

        print(_red("Response:"))
        print(_green(outputs[0].strip()))

        if len(outputs) > 1:
            choices_text = f" \t (and {len(outputs)-1} other completions)"
            print(_red(choices_text, end=""))

    print("\n\n\n")


############################


class LitellmModel(LM):
    """A wrapper class for LiteLLM.

    Check out https://docs.litellm.ai/docs/providers for usage details.
    """

    def __init__(
        self,
        model: str = "openai/gpt-4o-mini",
        api_key: Optional[str] = None,
        model_type: Literal["chat", "text"] = "chat",
        **kwargs,
    ):
        super().__init__(model=model, api_key=api_key, model_type=model_type, **kwargs)
        self._token_usage_lock = threading.Lock()
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def log_usage(self, response):
        """Log the total tokens from the OpenAI API response."""
        usage_data = response.get("usage")
        if usage_data:
            with self._token_usage_lock:
                self.prompt_tokens += usage_data.get("prompt_tokens", 0)
                self.completion_tokens += usage_data.get("completion_tokens", 0)

    def get_usage_and_reset(self):
        """Get the total tokens used and reset the token usage."""
        usage = {
            self.model
            or self.kwargs.get("model")
            or self.kwargs.get("engine"): {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
            }
        }
        self.prompt_tokens = 0
        self.completion_tokens = 0

        return usage

    # This is the only live LM wrapper, and upstream left it as the only one without a retry:
    # every other @backoff in this file is on a class deprecated after v1.1.0. A rate limit or a
    # dropped connection therefore failed the whole stage on the first try.
    @backoff.on_exception(
        backoff.expo,
        _RETRYABLE,
        max_time=1000,
        on_backoff=backoff_hdlr,
    )
    def _request(self, completion, payload):
        return completion(payload)

    def __call__(self, prompt=None, messages=None, **kwargs):
        # Build the request.
        cache = kwargs.pop("cache", self.cache)
        messages = messages or [{"role": "user", "content": prompt}]
        kwargs = {**self.kwargs, **kwargs}

        # Make the request and handle LRU & disk caching.
        if self.model_type == "chat":
            completion = cached_litellm_completion if cache else litellm_completion
        else:
            completion = (
                cached_litellm_text_completion if cache else litellm_text_completion
            )

        response = self._request(
            completion, ujson.dumps(dict(model=self.model, messages=messages, **kwargs))
        )
        response_dict = response.json()
        self.log_usage(response_dict)
        usage = response_dict.get("usage") or {}
        runtime.record_lm_usage(
            self.model,
            # Set by whatever built this model, so a run can be read as which stage spent what.
            getattr(self, "role", None),
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            # None on a cache hit, which costs nothing.
            response.get("_hidden_params", {}).get("response_cost") or 0.0,
        )
        outputs = [
            c.message.content if hasattr(c, "message") else c["text"]
            for c in response["choices"]
        ]

        # Logging, with removed api key & where `cost` is None on cache hit.
        kwargs = {k: v for k, v in kwargs.items() if not k.startswith("api_")}
        entry = dict(
            prompt=prompt, messages=messages, kwargs=kwargs, response=response_dict
        )
        entry = dict(**entry, outputs=outputs, usage=dict(response_dict["usage"]))
        entry = dict(
            **entry, cost=response.get("_hidden_params", {}).get("response_cost")
        )
        self.history.append(entry)

        return outputs
