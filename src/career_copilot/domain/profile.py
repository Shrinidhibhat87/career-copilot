"""The scoring profile. Data only — a second user is a second directory."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Profile:
    """Preferences and resume text. No identity, no file paths, no I/O."""

    preferences: str
    resume: str

    @property
    def is_empty(self) -> bool:
        return not self.preferences.strip() or not self.resume.strip()
