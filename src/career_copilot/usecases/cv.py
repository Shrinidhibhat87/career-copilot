"""Which CV a run scores against."""

from __future__ import annotations

from career_copilot.domain.cv import StoredCv
from career_copilot.ports import CvStore, ResumeMissingError

MISSING_MESSAGE = "No CV is stored yet. Upload one on the CV page."


def ensure_current_cv(store: CvStore) -> StoredCv:
    """The stored current CV. With none there is nothing to score against and that is an
    error, never a silent fallback to some other text."""
    current = store.current_cv()
    if current is None:
        raise ResumeMissingError(MISSING_MESSAGE)
    return current
