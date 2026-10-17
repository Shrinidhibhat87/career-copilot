"""Deterministic selection helpers for company-diverse result sets."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable


def cap_per_company[T](
    items: Iterable[T], limit: int, max_per_company: int, company: Callable[[T], str]
) -> list[T]:
    """Keep input order while enforcing a global per-company cap."""
    if limit <= 0:
        return []
    counts: Counter[str] = Counter()
    selected: list[T] = []
    for item in items:
        key = company(item)
        if counts[key] >= max_per_company:
            continue
        selected.append(item)
        counts[key] += 1
        if len(selected) == limit:
            break
    return selected
