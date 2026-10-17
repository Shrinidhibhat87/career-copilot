"""The chat under Recommendations: what a saved message looks like."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class FeedbackEntry:
    id: int
    role: str  # "user" or "assistant"
    text: str
    actions: str | None  # JSON list, on assistant rows
    created_at: datetime
