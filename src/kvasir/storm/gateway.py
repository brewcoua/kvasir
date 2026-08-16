"""One HTTP client for the OpenAI-compatible gateway.

Completions and embeddings are both a single POST, so this replaces litellm entirely. What litellm
was doing for us — routing, provider fallbacks, retries, response caching, cost accounting — is the
gateway's job in this deployment, and doing it twice only added a dependency that rewrote model
names on the way out.

Model names reach the gateway verbatim. litellm consumed the first `/`-separated segment as a
provider hint, which is why a name like `openai/ollama/model:cloud` needed explaining; nothing here
parses a model name.
"""

import httpx

# A completion can legitimately take minutes on a slow model, so the read timeout is generous while
# connecting is not: an unreachable gateway should fail fast rather than hold a pipeline thread.
_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=60.0)

_client = httpx.Client(timeout=_TIMEOUT)


class GatewayError(RuntimeError):
    """The gateway refused a request, or answered with something unusable."""


class RetryableGatewayError(GatewayError):
    """The gateway failed in a way a later attempt can plausibly fix: a rate limit or a 5xx.

    A bad request, an auth failure or a context-length overflow is deterministic, so it is raised as
    a plain `GatewayError` and fails immediately rather than after sixteen minutes of backoff.
    """


def post(path: str, api_base: str, api_key: str | None, payload: dict) -> httpx.Response:
    """POST `payload` to `{api_base}{path}` and return the response, raising on a failure status."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    response = _client.post(api_base.rstrip("/") + path, headers=headers, json=payload)

    if response.status_code == 429 or response.status_code >= 500:
        raise RetryableGatewayError(f"{response.status_code} from {path}: {response.text[:500]}")
    if response.status_code >= 400:
        raise GatewayError(f"{response.status_code} from {path}: {response.text[:500]}")
    return response


def response_cost(response: httpx.Response, body: dict) -> float:
    """What the gateway says the call cost, or 0.0 when it says nothing.

    Two gateways are supported and they report differently: a LiteLLM proxy sets a response header,
    Bifrost puts it in the usage object, either as a number or split into prompt and completion. A
    gateway that reports nothing leaves the run's cost at zero, which is not the same as free.
    """
    header = response.headers.get("x-litellm-response-cost")
    if header:
        try:
            return float(header)
        except ValueError:
            pass

    cost = (body.get("usage") or {}).get("cost")
    if isinstance(cost, int | float):
        return float(cost)
    if isinstance(cost, dict):
        return _number(cost.get("prompt_cost")) + _number(cost.get("completion_cost"))

    return _number((body.get("_hidden_params") or {}).get("response_cost"))


def _number(value: object) -> float:
    return float(value) if isinstance(value, int | float) else 0.0
