"""Loading and saving preferences. The only place a bootstrap is derived."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from career_copilot.domain.preferences import Preferences, validate
from career_copilot.ports import PreferencesStore


def load_preferences(store: PreferencesStore, seed: Preferences) -> Preferences:
    """The saved current version, or the caller's in-memory bootstrap.

    The bootstrap is never written. A database with no saved preferences therefore still
    runs the CLI correctly, and `version == 0` keeps meaning "config seed, never edited",
    which is what lets the API decide whether resume detections may still prefill.
    """
    return store.load_current() or seed


def save_preferences(
    store: PreferencesStore, prefs: Preferences, clock: Callable[[], datetime]
) -> Preferences:
    """Validate, then append a new version. The first save is version 1.

    Raises `PreferencesError` with one message per offending field; nothing is written
    when validation fails.
    """
    validate(prefs)
    return store.save(replace(prefs, version=0, updated_at=None), clock())
