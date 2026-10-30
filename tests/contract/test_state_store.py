"""Contract every StateStore adapter must satisfy.

Parametrize `store` when a second adapter (libSQL) arrives; the assertions
below do not change.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import FilterOutcome, JobStatus, ScoreStage, UpsertOutcome
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from tests.conftest import make_raw


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


def put(store: SqliteStore, now: datetime, **overrides: object) -> tuple[int, UpsertOutcome]:
    posting = to_posting(make_raw(**overrides), store.company_aliases())
    return store.upsert_job(posting, apply_hard_filters(posting), now)


def test_first_write_inserts(store: SqliteStore, now: datetime) -> None:
    _, outcome = put(store, now)
    assert outcome is UpsertOutcome.INSERTED


def test_same_job_twice_is_not_duplicated(store: SqliteStore, now: datetime) -> None:
    first_id, _ = put(store, now)
    second_id, outcome = put(store, now + timedelta(hours=1))
    assert (second_id, outcome) == (first_id, UpsertOutcome.UNCHANGED)
    assert len(store.list_jobs(limit=100)) == 1


def test_same_job_from_another_source_dedupes_on_fingerprint(
    store: SqliteStore, now: datetime
) -> None:
    put(store, now)
    _, outcome = put(
        store,
        now,
        source="lever",
        source_job_id="zzz",
        company="Acme",
        title="Forward Deployed Engineer (f/m/x)",
        location="Berlin",
        url="https://jobs.lever.co/acme/zzz",
    )
    assert outcome is UpsertOutcome.UNCHANGED
    assert len(store.list_jobs(limit=100)) == 1


def test_another_source_does_not_overwrite_the_owning_source_fields(
    store: SqliteStore, now: datetime
) -> None:
    """Cross-source wording differs without the job changing. Only liveness moves."""
    job_id, _ = put(store, now)
    later = now + timedelta(days=1)
    put(
        store,
        later,
        source="lever",
        source_job_id="zzz",
        company="Acme",
        title="Forward Deployed Engineer (f/m/x)",
        location="Berlin",
        url="https://jobs.lever.co/acme/zzz",
    )
    row = store._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["source"] == "greenhouse"
    assert row["company"] == "Acme GmbH"
    assert row["title"] == "Forward Deployed Engineer (m/w/d)"
    assert row["canonical_url"] == "https://boards.greenhouse.io/acme/jobs/1001"
    assert row["last_seen_at"] == later.isoformat()


def test_tracking_params_do_not_create_a_second_row(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    _, outcome = put(
        store,
        now,
        source_job_id="1001",
        url="https://boards.greenhouse.io/acme/jobs/1001?utm_source=twitter&ref=feed",
    )
    assert outcome is UpsertOutcome.UNCHANGED


def test_changed_description_is_flagged_and_versioned(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    _, outcome = put(store, now + timedelta(days=1), description="Rewritten posting, now Rust.")
    assert outcome is UpsertOutcome.CHANGED


def test_changed_metadata_is_stored_even_when_description_is_identical(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now)
    expected = to_posting(
        make_raw(
            title="Senior Forward Deployed Engineer",
            location="Munich, Germany",
            url="https://boards.greenhouse.io/acme/jobs/1001-senior",
            employment_type="contract",
        )
    )
    _, outcome = put(
        store,
        now + timedelta(days=1),
        title=expected.title,
        location=expected.location,
        url="https://boards.greenhouse.io/acme/jobs/1001-senior",
        employment_type="contract",
    )
    assert outcome is UpsertOutcome.CHANGED
    stored = store.list_jobs(limit=100)
    assert len(stored) == 1
    posting = stored[0].posting
    assert posting.title == expected.title
    assert posting.title_norm == expected.title_norm
    assert posting.location == expected.location
    assert posting.location_norm == expected.location_norm
    assert posting.canonical_url == expected.canonical_url
    assert posting.employment_type == "contract"
    assert posting.fingerprint == expected.fingerprint
    row = store._conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["needs_rescore"] == 1


def test_metadata_only_change_does_not_add_a_content_version(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now)
    put(store, now + timedelta(days=1), title="Staff Forward Deployed Engineer")
    versions = store._conn.execute(
        "SELECT COUNT(*) AS n FROM job_versions WHERE job_id = ?", (job_id,)
    ).fetchone()
    assert versions["n"] == 1


def test_reobservation_with_no_changes_stays_unchanged(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    _, outcome = put(store, now + timedelta(days=1))
    assert outcome is UpsertOutcome.UNCHANGED


def test_update_keeps_identity_columns_owned_by_another_row(
    store: SqliteStore, now: datetime
) -> None:
    """A fingerprint collision must not blow up the run on the unique index."""
    first_id, _ = put(store, now)
    other_id, _ = put(
        store,
        now,
        source_job_id="2000",
        title="Platform Engineer",
        url="https://boards.greenhouse.io/acme/jobs/2000",
    )
    taken = store._conn.execute(
        "SELECT fingerprint FROM jobs WHERE id = ?", (first_id,)
    ).fetchone()["fingerprint"]
    # Re-observe job 2000 renamed to the title job 1001 already owns.
    _, outcome = put(
        store,
        now + timedelta(days=1),
        source_job_id="2000",
        url="https://boards.greenhouse.io/acme/jobs/2000",
        employment_type="contract",
    )
    assert outcome is UpsertOutcome.CHANGED
    row = store._conn.execute("SELECT * FROM jobs WHERE id = ?", (other_id,)).fetchone()
    assert row["employment_type"] == "contract"
    assert row["fingerprint"] != taken  # kept: the other row owns that identity
    assert len(store.list_jobs(limit=100)) == 2


def test_last_seen_advances_on_reobservation(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    later = now + timedelta(days=2)
    put(store, later)
    assert store.list_jobs(limit=1)[0].last_seen_at == later


def test_first_seen_is_preserved(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    put(store, now + timedelta(days=2))
    assert store.list_jobs(limit=1)[0].first_seen_at == now


def test_jobs_are_listable_by_outcome(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    put(store, now, source_job_id="2", title="Head of AI", url="https://x.com/2")
    assert len(store.list_jobs(FilterOutcome.PASS, limit=100)) == 1
    assert len(store.list_jobs(FilterOutcome.REJECT, limit=100)) == 1


def test_stale_jobs_go_inactive_and_are_never_deleted(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    store.mark_inactive_before(now + timedelta(days=30))
    assert store.list_jobs(limit=100) == []
    rows = store._conn.execute("SELECT status FROM jobs").fetchall()
    assert [r["status"] for r in rows] == [str(JobStatus.INACTIVE)]


def test_cooldown_blocks_then_expires(store: SqliteStore, now: datetime) -> None:
    assert store.is_cooled_down("greenhouse", "acme", now) is True
    store.set_cooldown("greenhouse", "acme", now, hours=24)
    assert store.is_cooled_down("greenhouse", "acme", now + timedelta(hours=1)) is False
    assert store.is_cooled_down("greenhouse", "acme", now + timedelta(hours=25)) is True


def test_last_scraped_by_target_lists_every_visited_board(
    store: SqliteStore, now: datetime
) -> None:
    assert store.last_scraped_by_target() == {}
    store.set_cooldown("greenhouse", "acme", now, hours=24)
    store.set_cooldown("lever", "beta", now + timedelta(hours=1), hours=24, failed=True)
    assert store.last_scraped_by_target() == {
        ("greenhouse", "acme"): now,
        ("lever", "beta"): now + timedelta(hours=1),
    }


def test_board_visits_name_each_board_and_say_when_it_was_visited(
    store: SqliteStore, now: datetime
) -> None:
    store.add_manual_target("greenhouse", "acme", now, name="Acme Corp", open_roles=7)
    store.add_manual_target("lever", "beta", now)  # added before names were kept
    store.set_cooldown("greenhouse", "acme", now + timedelta(hours=2), hours=24)

    by_slug = {visit.slug: visit for visit in store.list_board_visits()}

    acme, beta = by_slug["acme"], by_slug["beta"]
    assert (acme.name, acme.origin, acme.open_roles) == ("Acme Corp", "manual", 7)
    assert acme.added_at == now
    assert acme.last_visited_at == now + timedelta(hours=2)
    assert (beta.name, beta.open_roles, beta.last_visited_at) == ("beta", None, None)


def test_failures_back_off_further_than_success(store: SqliteStore, now: datetime) -> None:
    store.set_cooldown("greenhouse", "acme", now, hours=1, failed=True)
    store.set_cooldown("greenhouse", "acme", now, hours=1, failed=True)
    assert store.is_cooled_down("greenhouse", "acme", now + timedelta(hours=3)) is False


def test_run_ledger_records_every_attempt(store: SqliteStore, now: datetime) -> None:
    run_id = store.start_run(now)
    store.record_source_run(run_id, "greenhouse", "acme", True, 5, None, now)
    store.record_source_run(run_id, "lever", "dead", False, 0, "boom", now)
    store.finish_run(run_id, now, "done")
    rows = store._conn.execute("SELECT ok, error FROM source_runs ORDER BY id").fetchall()
    assert [(r["ok"], r["error"]) for r in rows] == [(1, None), (0, "boom")]


def test_an_ad_hoc_run_records_its_kind(store: SqliteStore, now: datetime) -> None:
    run_id = store.start_run(now, kind="source_discovery")
    row = store.get_run(run_id)
    assert row is not None
    assert row["kind"] == "source_discovery"


def test_company_aliases_round_trip(store: SqliteStore) -> None:
    store.add_company_alias("dh", "delivery hero")
    assert store.company_aliases() == {"dh": "delivery hero"}


def test_migrations_are_idempotent(tmp_path: object) -> None:
    path = f"{tmp_path}/jobs.db"
    SqliteStore(path).close()
    store = SqliteStore(path)
    version = store._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    store.close()
    assert version == 32


def test_migration_v2_creates_scores_and_run_cost_columns(tmp_path: object) -> None:
    store = SqliteStore(f"{tmp_path}/jobs.db")
    try:
        version = store._conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        score_columns = {
            row["name"] for row in store._conn.execute("PRAGMA table_info(scores)").fetchall()
        }
        run_columns = {
            row["name"] for row in store._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
    finally:
        store.close()
    assert version == 32
    assert {"job_id", "stage", "score", "verdict", "rubric_version", "model"} <= score_columns
    assert {"llm_calls", "cost_eur"} <= run_columns


def make_score(job_id: int, content_hash: str, score: int = 80, model: str = "m") -> Score:
    return Score(
        job_id=job_id,
        stage=ScoreStage.MATCH,
        score=score,
        verdict=ScoreVerdict.from_score(score),
        rubric_version=RUBRIC_VERSION,
        model=model,
        prompt_hash="ph",
        content_hash=content_hash,
        reasons=("agentic AI ownership",),
        gaps=("no Kubernetes",),
        evidence_refs=("Built a LangGraph POC",),
    )


def content_hash_of(store: SqliteStore, job_id: int) -> str:
    row = store._conn.execute("SELECT content_hash FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return str(row["content_hash"])


def test_no_match_scores_on_a_fresh_store(store: SqliteStore) -> None:
    assert store.has_match_scores() is False


def test_has_match_scores_after_recording_one(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    assert store.has_match_scores() is True


def test_recording_the_same_score_twice_is_a_no_op(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    score = make_score(job_id, content_hash_of(store, job_id))
    first = store.record_score(score, now)
    second = store.record_score(score, now)
    assert first > 0
    assert second == 0
    rows = store._conn.execute("SELECT COUNT(*) AS n FROM scores").fetchone()
    assert rows["n"] == 1


def test_jobs_needing_score_excludes_already_scored(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    assert [j.id for j in store.jobs_needing_score("m", RUBRIC_VERSION, 10)] == [job_id]
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    assert store.jobs_needing_score("m", RUBRIC_VERSION, 10) == []


def test_a_changed_description_reopens_the_job_for_scoring(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    put(store, now + timedelta(days=1), description="Rewritten Python posting, now Rust.")
    assert [j.id for j in store.jobs_needing_score("m", RUBRIC_VERSION, 10)] == [job_id]


def test_a_new_model_reopens_the_job_for_scoring(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    assert store.jobs_needing_score("m", RUBRIC_VERSION, 10) == []
    assert [j.id for j in store.jobs_needing_score("other", RUBRIC_VERSION, 10)] == [job_id]


def test_a_rubric_bump_reopens_the_job_for_scoring(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    assert [j.id for j in store.jobs_needing_score("m", RUBRIC_VERSION + 1, 10)] == [job_id]


def test_rejected_jobs_are_never_scored(store: SqliteStore, now: datetime) -> None:
    put(store, now, source_job_id="2", title="Head of AI", url="https://x.com/2")
    titles = [j.posting.title for j in store.jobs_needing_score("m", RUBRIC_VERSION, 10)]
    assert "Head of AI" not in titles


def test_inactive_jobs_are_never_scored(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    store.mark_inactive_before(now + timedelta(days=30))
    assert store.jobs_needing_score("m", RUBRIC_VERSION, 10) == []


def test_match_scores_returns_the_score_with_its_job(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id), score=91), now)
    scored = store.match_scores(limit=10)
    assert len(scored) == 1
    assert scored[0].job.id == job_id
    assert scored[0].score.score == 91
    assert scored[0].score.reasons == ("agentic AI ownership",)
    assert scored[0].score.evidence_refs == ("Built a LangGraph POC",)


def test_match_scores_returns_only_the_newest_per_job(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id), score=50), now)
    put(store, now + timedelta(days=1), description="Rewritten Python posting, now Rust.")
    store.record_score(
        make_score(job_id, content_hash_of(store, job_id), score=95), now + timedelta(days=1)
    )
    scored = store.match_scores(limit=10)
    assert [s.score.score for s in scored] == [95]


def test_match_scores_excludes_a_score_for_old_content(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    put(store, now + timedelta(days=1), description="Rewritten posting, now Rust.")
    assert store.match_scores(limit=10) == []


def test_match_scores_excludes_jobs_that_now_fail_filters(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    put(store, now + timedelta(days=1), title="Head of AI")
    assert store.match_scores(limit=10) == []


def test_match_scores_caps_rows_per_company_before_the_limit(
    store: SqliteStore, now: datetime
) -> None:
    for index, score in enumerate((90, 80, 70)):
        job_id, _ = put(
            store,
            now,
            source_job_id=f"acme-{index}",
            title=f"Python Engineer {index}",
            url=f"https://boards.greenhouse.io/acme/jobs/{index}",
        )
        store.record_score(make_score(job_id, content_hash_of(store, job_id), score=score), now)
    other_id, _ = put(
        store,
        now,
        company="Globex",
        source_job_id="glo-1",
        title="Python Engineer",
        url="https://boards.greenhouse.io/globex/jobs/1",
    )
    store.record_score(make_score(other_id, content_hash_of(store, other_id), score=60), now)

    capped = store.match_scores(limit=10, max_per_company=2)
    assert [s.score.score for s in capped] == [90, 80, 60]
    # The limit still applies after the cap, and both are honoured together.
    assert [s.score.score for s in store.match_scores(limit=2, max_per_company=2)] == [90, 80]
    # Without a cap the query is unchanged.
    assert [s.score.score for s in store.match_scores(limit=10)] == [90, 80, 70, 60]


def test_match_scores_honours_the_current_model_and_rubric(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now)
    store.record_score(make_score(job_id, content_hash_of(store, job_id)), now)
    assert store.match_scores(limit=10, model="other") == []
    assert store.match_scores(limit=10, rubric_version=RUBRIC_VERSION + 1) == []
    assert store.digest_stats(model="other").scored_jobs == 0
    assert store.digest_stats(rubric_version=RUBRIC_VERSION + 1).scored_jobs == 0


def test_clear_needs_rescore(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.clear_needs_rescore(job_id)
    row = store._conn.execute("SELECT needs_rescore FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["needs_rescore"] == 0


def test_run_cost_is_recorded(store: SqliteStore, now: datetime) -> None:
    run_id = store.start_run(now)
    store.record_run_cost(run_id, calls=12, cost_eur=0.34)
    row = store._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert (row["llm_calls"], row["cost_eur"]) == (12, 0.34)


def test_has_jobs_is_false_on_a_fresh_store(store: SqliteStore) -> None:
    assert store.has_jobs() is False


def test_has_jobs_is_true_after_an_insert(store: SqliteStore, now: datetime) -> None:
    put(store, now)
    assert store.has_jobs() is True


# --- review -----------------------------------------------------------------


def score_for(
    store: SqliteStore,
    job_id: int,
    now: datetime,
    stage: ScoreStage = ScoreStage.MATCH,
    score: int = 80,
    **overrides: object,
) -> None:
    posting = store._conn.execute(
        "SELECT content_hash FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0]
    defaults: dict[str, object] = {
        "job_id": job_id,
        "stage": stage,
        "score": score,
        "verdict": ScoreVerdict.from_score(score),
        "rubric_version": RUBRIC_VERSION,
        "model": "test-model",
        "prompt_hash": "p",
        "content_hash": posting,
    }
    defaults.update(overrides)
    store.record_score(Score(**defaults), now, cost_eur=0.01)  # type: ignore[arg-type]


def items(page: dict[str, object]) -> list[dict[str, Any]]:
    """`review_page` answers HTTP, so its rows are plain dicts rather than a value type."""
    rows = page["items"]
    assert isinstance(rows, list)
    return rows


def test_the_review_list_ranks_by_the_score_that_exists(store: SqliteStore, now: datetime) -> None:
    """A triage score ranks a posting the match pass never reached; neither is a zero."""
    low, _ = put(store, now, source_job_id="1", url="https://x.test/1", title="Data Engineer A")
    high, _ = put(store, now, source_job_id="2", url="https://x.test/2", title="Data Engineer B")
    unscored, _ = put(
        store, now, source_job_id="3", url="https://x.test/3", title="Data Engineer C"
    )
    score_for(store, low, now, score=40)
    score_for(store, high, now, stage=ScoreStage.TRIAGE, score=90)

    page = store.review_page(threshold=30)
    assert [item["id"] for item in items(page)] == [high, low]
    assert [item["scoreStage"] for item in items(page)] == ["triage", "match"]
    assert (page["kept"], page["unscored"]) == (2, 1)

    everything = store.review_page(threshold=30, include_below=True)
    assert [item["id"] for item in items(everything)] == [high, low, unscored]


def test_an_unscored_posting_and_a_low_one_are_hidden_by_default(
    store: SqliteStore, now: datetime
) -> None:
    unscored, _ = put(
        store, now, source_job_id="1", url="https://x.test/1", title="Data Engineer A"
    )
    cut, _ = put(store, now, source_job_id="2", url="https://x.test/2", title="Data Engineer B")
    good, _ = put(store, now, source_job_id="3", url="https://x.test/3", title="Data Engineer C")
    score_for(store, cut, now, score=20)
    score_for(store, good, now, score=70)

    assert [item["id"] for item in items(store.review_page(threshold=50))] == [good]
    page = store.review_page(threshold=50)
    assert (page["kept"], page["below"], page["unscored"], page["dropped"]) == (1, 1, 1, 2)

    with_below = items(store.review_page(threshold=50, include_below=True))
    assert [item["id"] for item in with_below] == [good, cut, unscored]
    assert [item["id"] for item in with_below if item["belowThreshold"]] == [cut]


def test_the_review_list_is_capped_at_fifty(store: SqliteStore, now: datetime) -> None:
    for n in range(60):
        job_id, _ = put(
            store, now, source_job_id=str(n), url=f"https://x.test/{n}", title=f"Data Engineer {n}"
        )
        score_for(store, job_id, now, score=60)

    assert len(items(store.review_page(threshold=30))) == 10
    page = store.review_page(threshold=30, limit=500)
    assert (len(items(page)), page["kept"]) == (50, 60)
    assert len(items(store.review_page(threshold=30, include_below=True, limit=500))) == 50


def test_a_rejected_posting_is_dropped_but_still_reachable(
    store: SqliteStore, now: datetime
) -> None:
    put(store, now, source_job_id="1", url="https://x.test/1", title="Data Engineer A")
    put(store, now, source_job_id="2", url="https://x.test/2", title="Frontend Engineer")

    page = store.review_page(threshold=30)
    assert (page["kept"], page["unscored"], page["dropped"]) == (0, 1, 2)
    assert len(items(store.review_page(threshold=30, include_below=True))) == 2


def test_the_card_carries_the_claims_their_evidence_and_the_rules(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now, title="Data Engineer")
    score_for(
        store,
        job_id,
        now,
        reasons=("Ships Python data pipelines",),
        gaps=("No Databricks",),
        evidence_refs=("Built SQLDumpReader in Python",),
    )
    detail = store.job_detail(job_id)
    assert detail is not None
    assert detail["reasons"] == ["Ships Python data pipelines"]
    assert detail["evidence"] == ["Built SQLDumpReader in Python"]
    assert detail["gaps"] == ["No Databricks"]
    assert detail["outcome"] == str(FilterOutcome.PASS)
    assert detail["costEur"] == 0.01


def test_an_unknown_job_has_no_card(store: SqliteStore) -> None:
    assert store.job_detail(404) is None


def test_a_second_label_appends_and_wins(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now, title="Data Engineer")
    store.record_review(job_id, "maybe", "", now)
    store.record_review(job_id, "good_fit", "worth a call", now + timedelta(minutes=1))

    detail = store.job_detail(job_id)
    assert detail is not None
    assert (detail["label"], detail["note"]) == ("good_fit", "worth a call")
    rows = store._conn.execute("SELECT label FROM reviews ORDER BY id").fetchall()
    assert [row["label"] for row in rows] == ["maybe", "good_fit"]


def test_a_label_given_before_the_advert_changed_is_marked_stale(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now, title="Data Engineer")
    store.record_review(job_id, "good_fit", "", now)
    put(store, now + timedelta(days=1), title="Data Engineer", description="Rewritten. Python.")

    detail = store.job_detail(job_id)
    assert detail is not None
    assert detail["labelStale"] is True


def test_a_correction_needs_no_job(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now, title="Data Engineer")
    store.record_correction(job_id, "rule", "no profile-stack signal", "it names Python", now)
    store.record_correction(None, "batch", "", "too many SRE roles", now)

    rows = store._conn.execute("SELECT job_id, kind FROM corrections ORDER BY id").fetchall()
    assert [(row["job_id"], row["kind"]) for row in rows] == [(job_id, "rule"), (None, "batch")]


def test_a_review_of_an_unknown_job_is_refused(store: SqliteStore, now: datetime) -> None:
    with pytest.raises(LookupError):
        store.record_review(404, "good_fit", "", now)


def test_migrations_are_contiguous_from_one() -> None:
    from career_copilot.adapters.store import migrations

    assert [v for v, _ in migrations.MIGRATIONS] == list(range(1, len(migrations.MIGRATIONS) + 1))


def test_list_jobs_hides_dismissed(store: SqliteStore, now: datetime) -> None:
    kept, _ = put(
        store, now, source_job_id="keep", title="Keep Engineer", url="https://example.com/keep"
    )
    gone, _ = put(
        store, now, source_job_id="gone", title="Gone Engineer", url="https://example.com/gone"
    )
    store.record_decision(gone, "dismissed", "", now)
    assert [job.id for job in store.list_jobs(limit=100)] == [kept]


def test_list_jobs_shows_a_restored_job(store: SqliteStore, now: datetime) -> None:
    job_id, _ = put(store, now)
    store.record_decision(job_id, "dismissed", "", now)
    store.record_decision(job_id, "restored", "", now + timedelta(minutes=1))
    assert [job.id for job in store.list_jobs(limit=100)] == [job_id]


def test_an_outline_is_served_only_while_the_posting_is_unchanged(
    store: SqliteStore, now: datetime
) -> None:
    job_id, _ = put(store, now, description="Build the thing for the customers daily.")
    content_hash = store.get_job(job_id).posting.content_hash  # type: ignore[union-attr]
    assert store.get_outline(job_id) is None

    store.save_outline(job_id, content_hash, ["a"], ["b"], "m", now)
    assert store.get_outline(job_id) == (("a",), ("b",))

    store.save_outline(job_id, "an-older-hash", ["a"], ["b"], "m", now)
    assert store.get_outline(job_id) is None

    store.save_outline(job_id, content_hash, ["a"], ["b"], "m", now)
    store.clean_job(job_id, now)
    assert store.get_outline(job_id) is None
