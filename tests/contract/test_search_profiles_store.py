"""Store contract for search profiles: cached by inputs hash, newest is latest."""

from __future__ import annotations

from datetime import UTC, datetime

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.search_profile import SearchProfile

T = datetime(2026, 10, 7, tzinfo=UTC)


def test_profiles_are_found_by_hash_and_latest_is_newest() -> None:
    store = SqliteStore()
    assert store.latest_search_profile() is None
    store.save_search_profile("a", SearchProfile("first"), "m", T)
    store.save_search_profile("b", SearchProfile("second", ("fde",)), "m", T)
    assert store.search_profile("a") == SearchProfile("first")
    assert store.search_profile("missing") is None
    assert store.latest_search_profile() == SearchProfile("second", ("fde",))


def test_resaving_a_hash_replaces_it_and_makes_it_latest() -> None:
    store = SqliteStore()
    store.save_search_profile("a", SearchProfile("first"), "m", T)
    store.save_search_profile("b", SearchProfile("second"), "m", T)
    store.save_search_profile("a", SearchProfile("first again"), "m", T)
    assert store.latest_search_profile() == SearchProfile("first again")
    count = store._conn.execute("SELECT COUNT(*) FROM search_profiles").fetchone()[0]
    assert count == 2


def test_a_saved_preference_never_carries_the_profile() -> None:
    store = SqliteStore()
    saved = store.save(Preferences(allowed_cities=("berlin",), search_profile="p"), T)
    assert saved.search_profile == ""
    current = store.load_current()
    assert current is not None and current.search_profile == ""
