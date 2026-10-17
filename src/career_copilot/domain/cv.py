"""A stored CV: the text a user uploaded, identified by what it says and not how it is packed."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


def normalize_cv_text(text: str) -> str:
    """Whitespace-collapsed text, so a PDF re-export that changes only line breaks or
    spacing is the same CV."""
    return " ".join(text.split())


def cv_sha256(text: str) -> str:
    return hashlib.sha256(normalize_cv_text(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredCv:
    """One immutable version. `text` is what the upload extracted, never the original bytes."""

    version: int
    sha256: str
    filename: str
    text: str
    uploaded_at: datetime
    current: bool = True


@dataclass(frozen=True, slots=True)
class SavedCv:
    """The outcome of an upload. `changed` is false for a byte-for-byte-equivalent re-upload
    of the CV that is already current."""

    cv: StoredCv
    changed: bool
