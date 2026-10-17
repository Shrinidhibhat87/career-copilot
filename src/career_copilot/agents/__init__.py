"""Agents: the only components that call a model. Leaves, never control flow."""

from __future__ import annotations

import hashlib


def prompt_hash(text: str) -> str:
    """Stamps a score with the exact prompt that produced it."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def as_tuple(value: object) -> tuple[str, ...]:
    """Model list fields arrive as lists; the domain holds tuples of strings."""
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def clamp(value: object, low: int = 0, high: int = 100) -> int:
    """A model can return anything. The band is fixed."""
    if not isinstance(value, int | float | str):
        return low
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))
