from __future__ import annotations

from pathlib import Path

from career_copilot.adapters.profile_loader import load_notes
from career_copilot.domain.profile import Profile


def test_load_notes_reads_the_preferences_file(tmp_path: Path) -> None:
    (tmp_path / "preferences.md").write_text("# Preferences\nBerlin, permanent.\n")
    assert "Berlin, permanent." in load_notes(tmp_path)


def test_load_notes_without_a_file_is_empty(tmp_path: Path) -> None:
    assert load_notes(tmp_path / "nope") == ""


def test_empty_profile_is_detected() -> None:
    assert Profile(preferences="  ", resume="").is_empty is True
