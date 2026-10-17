"""A dismissal follows a repost: same company, a close description, a new id."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.usecases.repost_guard import hide_reposts
from tests.conftest import make_raw

THRESHOLD = 0.9


class FakeEmbedder:
    """Two directions: texts that mention rust, and everything else."""

    model = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [[1.0, 0.1] if "rust" in text.lower() else [0.1, 1.0] for text in texts]


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


def put(store: SqliteStore, now: datetime, key: str, **overrides: object) -> int:
    fields: dict[str, object] = {
        "source_job_id": key,
        "url": f"https://example.com/{key}",
        "description": "Rust payments platform, permanent role.",
    }
    fields.update(overrides)
    posting = to_posting(make_raw(**fields), store.company_aliases())
    job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), now)
    return job_id


def visible(store: SqliteStore) -> set[int]:
    return {job.id for job in store.list_jobs(limit=100)}


def guard(store: SqliteStore, embedder: FakeEmbedder, ids: list[int], now: datetime) -> int:
    return hide_reposts(store, lambda: embedder, ids, THRESHOLD, now)


def test_a_reworded_repost_of_a_dismissed_job_is_hidden(store: SqliteStore, now: datetime) -> None:
    old = put(store, now, "a", title="Backend Engineer")
    store.record_decision(old, "dismissed", "", now)
    repost = put(store, now, "b", title="Senior Backend Engineer (m/w/d)")

    assert guard(store, FakeEmbedder(), [repost], now) == 1
    assert repost not in visible(store)
    assert store.repost_of(repost) == old


def test_a_different_company_is_not_hidden(store: SqliteStore, now: datetime) -> None:
    old = put(store, now, "a", title="Backend Engineer")
    store.record_decision(old, "dismissed", "", now)
    other = put(store, now, "b", title="Backend Engineer", company="Globex AG")

    assert guard(store, FakeEmbedder(), [other], now) == 0
    assert other in visible(store)


def test_a_different_role_at_the_same_company_is_not_hidden(
    store: SqliteStore, now: datetime
) -> None:
    old = put(store, now, "a", title="Backend Engineer")
    store.record_decision(old, "dismissed", "", now)
    other = put(store, now, "b", title="Office Manager", description="Run the Berlin office.")

    assert guard(store, FakeEmbedder(), [other], now) == 0
    assert other in visible(store)


def test_nothing_dismissed_means_the_embedder_is_never_built(
    store: SqliteStore, now: datetime
) -> None:
    fresh = put(store, now, "a")

    def build() -> FakeEmbedder:
        raise AssertionError("embedder built with nothing to compare")

    assert hide_reposts(store, build, [fresh], THRESHOLD, now) == 0


def test_an_embedder_failure_leaves_the_job_visible(store: SqliteStore, now: datetime) -> None:
    old = put(store, now, "a")
    store.record_decision(old, "dismissed", "", now)
    repost = put(store, now, "b", title="Backend Engineer II")

    def broken() -> FakeEmbedder:
        raise RuntimeError("model not available")

    assert hide_reposts(store, broken, [repost], THRESHOLD, now) == 0
    assert repost in visible(store)


def test_a_restored_original_does_not_hide_its_repost(store: SqliteStore, now: datetime) -> None:
    old = put(store, now, "a")
    store.record_decision(old, "dismissed", "", now)
    store.record_decision(old, "restored", "", now)
    repost = put(store, now, "b", title="Backend Engineer II")

    assert guard(store, FakeEmbedder(), [repost], now) == 0


def test_a_cleaned_original_still_hides_a_repost_with_the_same_title(
    store: SqliteStore, now: datetime
) -> None:
    old = put(store, now, "a", title="Backend Engineer")
    store.record_decision(old, "dismissed", "", now)
    store.clean_job(old, now)
    repost = put(
        store,
        now,
        "b",
        title="Backend Engineer",
        location="Munich, Germany",
        description="Reworded text.",
    )
    assert repost != old

    assert guard(store, FakeEmbedder(), [repost], now) == 1
    assert store.repost_of(repost) == old
