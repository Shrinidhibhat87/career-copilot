"""Reads the preferences seed off disk. The domain stays I/O-free, so the reading lives here."""

from __future__ import annotations

from pathlib import Path

PREFERENCES_FILE = "preferences.md"


def load_notes(profile_dir: str | Path) -> str:
    """The prose preferences file, or an empty string when there is none.

    A missing file is not an error: this reads the *bootstrap seed* for the notes
    field, and once preferences have been saved to the
    store the file is no longer part of the run at all.
    """
    preferences = Path(profile_dir) / PREFERENCES_FILE
    return preferences.read_text(encoding="utf-8") if preferences.is_file() else ""
