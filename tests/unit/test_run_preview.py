from __future__ import annotations

from datetime import UTC, datetime

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preferences import Preferences
from career_copilot.usecases.pipeline import preview_score
from tests.conftest import make_raw


def test_preview_is_read_only_and_candidate_changes_do_not_stale_its_token() -> None:
    now = datetime(2026, 9, 6, tzinfo=UTC)
    config = Config()
    prefs = Preferences(core_skills=("python",))
    with SqliteStore(":memory:") as store:
        before = store._conn.total_changes
        first = preview_score(config, store, prefs)
        assert store._conn.total_changes == before
        posting = to_posting(make_raw())
        store.upsert_job(posting, apply_hard_filters(posting), now)
        second = preview_score(config, store, prefs)
        assert second.candidates_now == first.candidates_now + 1
        assert second.token == first.token
        assert store.list_runs() == []


def test_preferences_and_model_changes_invalidate_a_preview() -> None:
    config = Config()
    prefs = Preferences(core_skills=("python",))
    with SqliteStore(":memory:") as store:
        original = preview_score(config, store, prefs)
        changed_prefs = preview_score(config, store, Preferences(core_skills=("rust",), version=1))
        config.scoring.match_model = "another-model"
        changed_model = preview_score(config, store, prefs)
    assert len({original.token, changed_prefs.token, changed_model.token}) == 3
