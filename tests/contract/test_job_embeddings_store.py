from datetime import UTC, datetime

from career_copilot.adapters.store.sqlite_store import SqliteStore

T = datetime(2026, 10, 7, tzinfo=UTC)


def test_vectors_round_trip_exactly() -> None:
    # These values are exact in float32, so storing as `array('f')` loses nothing.
    with SqliteStore() as store:
        store.save_job_vectors("m", [("h1", [0.5, -0.25, 1.0])], T)
        assert store.job_vectors("m", ["h1", "absent"]) == {"h1": [0.5, -0.25, 1.0]}


def test_each_model_has_its_own_cache() -> None:
    with SqliteStore() as store:
        store.save_job_vectors("a", [("h1", [1.0, 0.0])], T)
        store.save_job_vectors("b", [("h1", [0.0, 1.0])], T)
        assert store.job_vectors("a", ["h1"]) == {"h1": [1.0, 0.0]}
        assert store.job_vectors("b", ["h1"]) == {"h1": [0.0, 1.0]}
        assert store.job_vectors("c", ["h1"]) == {}


def test_saving_again_replaces_the_vector() -> None:
    with SqliteStore() as store:
        store.save_job_vectors("m", [("h1", [1.0])], T)
        store.save_job_vectors("m", [("h1", [2.0])], T)
        assert store.job_vectors("m", ["h1"]) == {"h1": [2.0]}


def test_more_hashes_than_one_query_can_bind() -> None:
    hashes = [f"h{i}" for i in range(1_234)]
    with SqliteStore() as store:
        store.save_job_vectors("m", [(h, [float(i)]) for i, h in enumerate(hashes)], T)
        found = store.job_vectors("m", [*hashes, *hashes])  # duplicates are harmless
    assert len(found) == 1_234
    assert found["h1233"] == [1233.0]


def test_vectors_survive_a_reopen(tmp_path: object) -> None:
    path = f"{tmp_path}/jobs.db"
    with SqliteStore(path) as store:
        store.save_job_vectors("m", [("h1", [0.5])], T)
    with SqliteStore(path) as store:
        assert store.job_vectors("m", ["h1"]) == {"h1": [0.5]}
