"""`SearchProvider` over the Tavily HTTP API.

Search is the one outward, paid call the discovery agent makes. Everything else it
does is verification against a public board API, which is free.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import httpx

from career_copilot.ports import FailureCategory, LLMCallError, SearchResult

API_URL = "https://api.tavily.com/search"
TIMEOUT = 20.0
KEY_ENV = "TAVILY_API_KEY"


class TavilySearch:
    """One instance per run. A transport failure is an `LLMCallError`, as everywhere."""

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        url: str = API_URL,
    ) -> None:
        self.api_key = api_key or os.environ.get(KEY_ENV, "")
        self._client = client or httpx.Client()
        self._url = url

    def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        try:
            response = self._client.post(
                self._url,
                json={
                    "api_key": self.api_key,
                    "query": query,
                    "max_results": max_results,
                    "search_depth": "basic",
                },
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise LLMCallError(
                f"search provider unreachable ({type(exc).__name__})",
                category=FailureCategory.TRANSPORT,
            ) from None
        except ValueError:
            raise LLMCallError(
                "search provider returned no usable answer",
                category=FailureCategory.PROVIDER,
            ) from None
        results = payload.get("results") or []
        return [
            SearchResult(
                title=str(item.get("title", "")),
                url=str(item.get("url", "")),
                snippet=str(item.get("content", ""))[:400],
            )
            for item in results
        ]
