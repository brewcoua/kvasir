"""Retrieval.

Upstream shipped eleven retrievers, nine of them behind a paid credential. Only SearXNG is kept:
it is what kvasir deploys against, and it needs no key. The rest are recoverable from upstream at
the fork point in NOTICE.
"""

import logging
from typing import Callable, Union, List

import dspy
import httpx

from . import runtime

logger = logging.getLogger(__name__)


class SearXNG(dspy.Retrieve):
    def __init__(
        self,
        searxng_api_url,
        searxng_api_key=None,
        k=3,
        is_valid_source: Callable = None,
    ):
        """Initialize the SearXNG search retriever.
        Please set up SearXNG according to https://docs.searxng.org/index.html.

        Args:
            searxng_api_url (str): The URL of the SearXNG API. Consult SearXNG documentation for details.
            searxng_api_key (str, optional): The API key for the SearXNG API. Defaults to None. Consult SearXNG documentation for details.
            k (int, optional): The number of top passages to retrieve. Defaults to 3.
            is_valid_source (Callable, optional): A function that takes a URL and returns a boolean indicating if the
            source is valid. Defaults to None.
        """
        super().__init__(k=k)
        if not searxng_api_url:
            raise RuntimeError("You must supply searxng_api_url")
        self.searxng_api_url = searxng_api_url
        self.searxng_api_key = searxng_api_key
        self.usage = 0
        # Why the last call's queries came back empty, if they failed rather than found nothing.
        # Read by the deep research pipeline, which searches one query at a time.
        self.failures: list[str] = []

        if is_valid_source:
            self.is_valid_source = is_valid_source
        else:
            self.is_valid_source = lambda x: True

    def get_usage_and_reset(self):
        usage = self.usage
        self.usage = 0
        return {"SearXNG": usage}

    def forward(
        self, query_or_queries: Union[str, List[str]], exclude_urls: List[str] = []
    ):
        """Search with SearxNG for self.k top passages for query or queries

        Args:
            query_or_queries (Union[str, List[str]]): The query or queries to search for.
            exclude_urls (List[str]): A list of urls to exclude from the search results.

        Returns:
            a list of Dicts, each dict has keys of 'description', 'snippets' (list of strings), 'title', 'url'
        """
        queries = (
            [query_or_queries]
            if isinstance(query_or_queries, str)
            else query_or_queries
        )
        self.usage += len(queries)
        self.failures = []
        collected_results = []
        headers = (
            {"Authorization": f"Bearer {self.searxng_api_key}"}
            if self.searxng_api_key
            else {}
        )

        for query in queries:
            search = runtime.search_started("SearXNG", query)
            rows = []
            failures = []
            try:
                params = {"q": query, "format": "json"}
                # SearXNG serves the snippets directly, so this request is the leaf of the
                # pipeline's nested pools and the only outbound call the retrieval level makes.
                with runtime.fetch_slot():
                    # A timeout, because upstream had none: a stalled instance held a pipeline
                    # thread and one of the process's fetch permits indefinitely.
                    # SearXNG answers its base URL with a 308 to /search, and httpx does not
                    # follow redirects unless asked, which left every query reading an HTML page.
                    response = httpx.get(
                        self.searxng_api_url,
                        headers=headers,
                        params=params,
                        timeout=30.0,
                        follow_redirects=True,
                    )
                results = response.json()
                rows = results["results"]
                if not rows:
                    # Rate limits and CAPTCHAs leave the engine out and the answer empty.
                    failures = [
                        f"{engine}: {reason}"
                        for engine, reason in results.get("unresponsive_engines", [])
                    ]

                for r in rows:
                    if self.is_valid_source(r["url"]) and r["url"] not in exclude_urls:
                        collected_results.append(
                            {
                                "description": r.get("content", ""),
                                "snippets": [r.get("content", "")],
                                "title": r.get("title", ""),
                                "url": r["url"],
                            }
                        )
            except Exception as e:
                logger.error(f"Error occurs when searching query {query}: {e}")
                failures = [f"{type(e).__name__}: {e}"]
            self.failures += failures
            runtime.search_ended(search, rows, failures)

        return collected_results
