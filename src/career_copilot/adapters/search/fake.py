"""Recorded search results, keyed by query substring.

Mirrors `adapters/llm/fake.py`, which scripts by prompt substring: a test says what the
web would have said, and the run is then deterministic and offline.
"""

from __future__ import annotations

from collections.abc import Sequence

from career_copilot.ports import SearchResult


class FakeSearch:
    def __init__(self, responses: dict[str, Sequence[SearchResult]] | None = None) -> None:
        self._responses = responses or {}
        self.queries: list[str] = []

    def search(self, query: str, max_results: int) -> Sequence[SearchResult]:
        self.queries.append(query)
        for needle, results in self._responses.items():
            if needle.lower() in query.lower():
                return list(results)[:max_results]
        return []
