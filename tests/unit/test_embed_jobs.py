from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from career_copilot.adapters.embed.fake import FakeEmbedder
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.usecases.embed_jobs import BATCH, ensure_job_vectors, job_text
from tests.conftest import make_raw

T = datetime(2026, 10, 7, tzinfo=UTC)


def seed(store: SqliteStore, **overrides: object) -> None:
    posting = to_posting(make_raw(**overrides))
    store.upsert_job(posting, apply_hard_filters(posting), T)


def embedded(embedder: FakeEmbedder) -> int:
    return sum(len(c) for c in embedder.calls)


def test_each_text_is_embedded_once_and_cached(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    embedder = FakeEmbedder()
    with SqliteStore(path) as store:
        seed(store, source_job_id="1", url="https://x/1")
        seed(store, source_job_id="2", url="https://x/2", title="Data Engineer")
        jobs = store.all_active_jobs()
        first = ensure_job_vectors(store, embedder, jobs, T)
    assert set(first) == {j.id for j in jobs}
    assert embedded(embedder) == 2
    with SqliteStore(path) as store:  # survives a reopen
        again = ensure_job_vectors(store, embedder, store.all_active_jobs(), T)
    assert embedded(embedder) == 2
    assert again == first


def test_edited_posting_is_re_embedded(tmp_path: Path) -> None:
    embedder = FakeEmbedder()
    with SqliteStore(tmp_path / "jobs.db") as store:
        seed(store)
        before = ensure_job_vectors(store, embedder, store.all_active_jobs(), T)
        seed(store, description="Now a completely different role in sales.")
        jobs = store.all_active_jobs()
        after = ensure_job_vectors(store, embedder, jobs, T)
    assert len(jobs) == 1
    assert embedded(embedder) == 2
    assert after[jobs[0].id] != before[jobs[0].id]


def test_two_jobs_with_one_text_are_embedded_once() -> None:
    embedder = FakeEmbedder()
    with SqliteStore() as store:
        seed(store)
        (job,) = store.all_active_jobs()
        twin = replace(job, id=99)
        vectors = ensure_job_vectors(store, embedder, [job, twin], T)
    assert embedder.calls == [[job_text(job.posting)]]
    assert vectors[job.id] == vectors[99]


def test_nothing_to_embed_makes_no_call() -> None:
    embedder = FakeEmbedder()
    with SqliteStore() as store:
        assert ensure_job_vectors(store, embedder, [], T) == {}
    assert embedder.calls == []


def test_job_text_caps_a_long_description() -> None:
    posting = to_posting(make_raw(description="x" * 10_000))
    assert len(job_text(posting)) < 2_200
    assert job_text(posting).startswith(posting.title)


def test_a_retitled_posting_is_re_embedded() -> None:
    # The description, and so `content_hash`, is unchanged; the text the model reads is not.
    embedder = FakeEmbedder()
    with SqliteStore() as store:
        seed(store)
        (job,) = store.all_active_jobs()
        retitled = replace(job, posting=replace(job.posting, title="Account Executive"))
        before = ensure_job_vectors(store, embedder, [job], T)
        after = ensure_job_vectors(store, embedder, [retitled], T)
    assert embedded(embedder) == 2
    assert before[job.id] != after[job.id]


def test_a_large_pool_is_embedded_in_batches() -> None:
    embedder = FakeEmbedder()
    with SqliteStore() as store:
        seed(store)
        (job,) = store.all_active_jobs()
        pool = [
            replace(job, id=i, posting=replace(job.posting, title=f"Role {i}"))
            for i in range(BATCH + 1)
        ]
        vectors = ensure_job_vectors(store, embedder, pool, T)
    assert [len(c) for c in embedder.calls] == [BATCH, 1]
    assert set(vectors) == set(range(BATCH + 1))
