from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.notify.markdown import MarkdownNotifier
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.match import MatchAgent
from career_copilot.agents.triage import TriageAgent
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.profile import Profile
from career_copilot.domain.scoring import RUBRIC_VERSION
from career_copilot.ports import (
    LLMCallError,
    LLMClient,
    LLMResponse,
    ResponseValidator,
    SchemaError,
)
from career_copilot.usecases.digest import DigestRun
from career_copilot.usecases.discover import DiscoverRun
from career_copilot.usecases.score import ScoreBudget, ScoreRun
from tests.conftest import FakeSource, make_raw
from tests.match_replies import match_reply, reply_with_total

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
PROFILE = Profile(preferences="Forward-deployed AI roles in Berlin.", resume="Built LangGraph POC")

MATCH_REPLY: dict[str, object] = reply_with_total(80, reasons=["agentic AI ownership"])


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


def seed(store: SqliteStore, count: int, now: datetime = NOW) -> None:
    for index in range(count):
        posting = to_posting(
            make_raw(
                source_job_id=str(1000 + index),
                title=f"Forward Deployed Engineer {index}",
                url=f"https://boards.greenhouse.io/acme/jobs/{1000 + index}",
            )
        )
        store.upsert_job(posting, apply_hard_filters(posting), now)


def build_run(
    store: SqliteStore,
    triage_client: LLMClient,
    match_client: LLMClient,
    target: int,
    max_eur: float = 0.0,
    dry_run: bool = False,
    clock_now: datetime = NOW,
    triage_reserve_eur: float = 0.0,
    match_reserve_eur: float = 0.0,
    max_per_company: int = 100,
) -> ScoreRun:
    return ScoreRun(
        store=store,
        triage=TriageAgent(triage_client, PROFILE.preferences),
        match=MatchAgent(match_client, PROFILE),
        clock=lambda: clock_now,
        budget=ScoreBudget(
            target=target,
            max_eur=max_eur,
            mode="test",
            triage_reserve_eur=triage_reserve_eur,
            match_reserve_eur=match_reserve_eur,
        ),
        rubric_version=RUBRIC_VERSION,
        dry_run=dry_run,
        max_per_company=max_per_company,
    )


class FailingLLM:
    model = "failing"

    def __init__(self, error: LLMCallError) -> None:
        self.error = error

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse:
        raise self.error


def test_triage_runs_on_every_survivor_but_match_only_on_the_budget(store: SqliteStore) -> None:
    seed(store, 10)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"})
    match = FakeLLM(default=MATCH_REPLY)
    report = build_run(store, triage, match, target=3).execute()
    assert report.triaged == 10
    assert report.matched == 3
    assert len(match.calls) == 3


def test_triage_is_not_silently_truncated_at_two_hundred_candidates(
    store: SqliteStore,
) -> None:
    seed(store, 205)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"})
    report = build_run(store, triage, FakeLLM(default=MATCH_REPLY), target=1).execute()
    assert report.triaged == 205
    assert len(triage.calls) == 205


def test_match_goes_to_the_highest_triage_scores(store: SqliteStore) -> None:
    seed(store, 5)
    triage = FakeLLM(default={"relevance": 10, "reason": "meh"})
    triage.script("Forward Deployed Engineer 3", {"relevance": 99, "reason": "exact match"})
    match = FakeLLM(default=MATCH_REPLY)
    build_run(store, triage, match, target=1).execute()
    assert "Forward Deployed Engineer 3" in match.calls[0]


def test_match_selection_caps_each_company(store: SqliteStore) -> None:
    seed(store, 10)
    for index in range(2):
        posting = to_posting(
            make_raw(
                source_job_id=f"beta-{index}",
                company="Beta GmbH",
                title=f"Python Engineer {index}",
                url=f"https://beta.example/{index}",
            )
        )
        store.upsert_job(posting, apply_hard_filters(posting), NOW)
    match = FakeLLM(default=MATCH_REPLY)
    report = build_run(
        store,
        FakeLLM(default={"relevance": 60, "reason": "fits"}),
        match,
        target=10,
        max_per_company=2,
    ).execute()
    assert report.matched == 4


def test_a_second_run_scores_nothing(store: SqliteStore) -> None:
    seed(store, 3)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"})
    match = FakeLLM(default=MATCH_REPLY)
    build_run(store, triage, match, target=10).execute()
    second = build_run(store, triage, match, target=10).execute()
    assert (second.triaged, second.matched) == (0, 0)


def test_a_changed_posting_is_rescored(store: SqliteStore) -> None:
    seed(store, 1)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"})
    match = FakeLLM(default=MATCH_REPLY)
    build_run(store, triage, match, target=10).execute()
    seed_now = NOW + timedelta(days=1)
    posting = to_posting(
        make_raw(
            source_job_id="1000",
            title="Forward Deployed Engineer 0",
            url="https://boards.greenhouse.io/acme/jobs/1000",
            description="Rewritten posting, now Rust.",
        )
    )
    store.upsert_job(posting, apply_hard_filters(posting), seed_now)
    report = build_run(store, triage, match, target=10, clock_now=seed_now).execute()
    assert report.matched == 0


def test_needs_rescore_is_cleared_after_a_match(store: SqliteStore) -> None:
    seed(store, 1)
    build_run(
        store,
        FakeLLM(default={"relevance": 60, "reason": "fits"}),
        FakeLLM(default=MATCH_REPLY),
        target=10,
    ).execute()
    row = store._conn.execute("SELECT needs_rescore FROM jobs").fetchone()
    assert row["needs_rescore"] == 0


def test_a_job_with_no_evidence_is_parked_and_the_run_continues(store: SqliteStore) -> None:
    seed(store, 2)
    match = FakeLLM(default=MATCH_REPLY)
    match.script(
        "Forward Deployed Engineer 0",
        reply_with_total(90, reasons=["trust me"], evidence_refs=[]),
    )
    report = build_run(
        store, FakeLLM(default={"relevance": 60, "reason": "fits"}), match, target=10
    ).execute()
    assert report.matched == 1
    assert report.failed == 1


def test_the_euro_ceiling_aborts_the_run(store: SqliteStore) -> None:
    seed(store, 10)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"}, cost_per_call_eur=0.5)
    match = FakeLLM(default=MATCH_REPLY)
    report = build_run(store, triage, match, target=10, max_eur=1.0).execute()
    assert report.aborted is True
    assert report.matched == 0
    assert "ceiling" in report.abort_reason


def test_dry_run_makes_no_model_call(store: SqliteStore) -> None:
    seed(store, 4)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"})
    match = FakeLLM(default=MATCH_REPLY)
    report = build_run(store, triage, match, target=2, dry_run=True).execute()
    assert (triage.calls, match.calls) == ([], [])
    assert report.candidates == 4
    assert (report.triaged, report.matched) == (0, 0)
    assert report.estimated_calls == 6
    assert report.cost_ceiling_eur == 0.0


def test_a_call_is_refused_before_spending_when_its_reservation_exceeds_the_cap(
    store: SqliteStore,
) -> None:
    seed(store, 1)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"})
    report = build_run(
        store,
        triage,
        FakeLLM(default=MATCH_REPLY),
        target=1,
        max_eur=0.01,
        triage_reserve_eur=0.02,
    ).execute()
    assert report.aborted is True
    assert report.cost_eur == 0.0
    assert triage.calls == []
    assert "next call reserves" in report.abort_reason


def test_schema_failures_record_billable_attempts_and_continue(store: SqliteStore) -> None:
    seed(store, 1)
    report = build_run(
        store,
        FailingLLM(SchemaError("bad response", calls=2, cost_eur=0.02)),
        FakeLLM(default=MATCH_REPLY),
        target=1,
    ).execute()
    row = store._conn.execute("SELECT ended_at, llm_calls, cost_eur FROM runs").fetchone()
    assert report.failed == 1
    assert report.llm_calls == 2
    assert report.cost_eur == pytest.approx(0.02)
    assert row["ended_at"] is not None
    assert (row["llm_calls"], row["cost_eur"]) == (2, pytest.approx(0.02))


def test_transport_failures_abort_and_finalize_the_run(store: SqliteStore) -> None:
    seed(store, 1)
    report = build_run(
        store,
        FailingLLM(LLMCallError("OpenRouter unavailable", calls=1)),
        FakeLLM(default=MATCH_REPLY),
        target=1,
    ).execute()
    row = store._conn.execute("SELECT ended_at, summary, llm_calls FROM runs").fetchone()
    assert report.aborted is True
    assert "OpenRouter unavailable" in report.abort_reason
    assert row["ended_at"] is not None
    assert "aborted" in row["summary"]
    assert row["llm_calls"] == 1


def test_the_run_cost_is_recorded(store: SqliteStore) -> None:
    seed(store, 2)
    triage = FakeLLM(default={"relevance": 60, "reason": "fits"}, cost_per_call_eur=0.01)
    match = FakeLLM(default=MATCH_REPLY, cost_per_call_eur=0.05)
    report = build_run(store, triage, match, target=2).execute()
    row = store._conn.execute(
        "SELECT llm_calls, cost_eur FROM runs WHERE id = ?", (report.run_id,)
    ).fetchone()
    assert row["llm_calls"] == 4
    assert row["cost_eur"] == pytest.approx(0.12)


def test_the_full_pipeline_is_idempotent(tmp_path: Path) -> None:
    """discover → score → digest, twice. The second pass must do nothing new."""
    db = tmp_path / "jobs.db"
    raws = [
        make_raw(
            source_job_id=str(2000 + i),
            title=f"Forward Deployed Engineer {i}",
            url=f"https://boards.greenhouse.io/acme/jobs/{2000 + i}",
        )
        for i in range(4)
    ]

    def pipeline(out: Path) -> tuple[int, int, str]:
        store = SqliteStore(db)
        try:
            discover = DiscoverRun(
                sources={"greenhouse": FakeSource("greenhouse", raws)},
                store=store,
                targets=[SourceTarget(source="greenhouse", identifier="acme", tier=0)],
                clock=lambda: NOW,
                stop_after=50,
            ).execute()
            scored = build_run(
                store,
                FakeLLM(default={"relevance": 60, "reason": "fits"}),
                FakeLLM(default=MATCH_REPLY),
                target=10,
            ).execute()
            body = DigestRun(store, MarkdownNotifier(out), clock=lambda: NOW).execute()
        finally:
            store.close()
        return discover.inserted, scored.matched, body

    first_inserted, first_matched, first_body = pipeline(tmp_path / "a.md")
    second_inserted, second_matched, second_body = pipeline(tmp_path / "b.md")

    assert (first_inserted, first_matched) == (4, 4)
    assert (second_inserted, second_matched) == (0, 0)
    assert first_body == second_body


def _threshold_run(
    store: SqliteStore, match: FakeLLM, target: int, min_score: int, cv_sha256: str | None = None
) -> ScoreRun:
    return ScoreRun(
        store=store,
        triage=TriageAgent(
            FakeLLM(default={"relevance": 60, "reason": "fits"}), PROFILE.preferences
        ),
        match=MatchAgent(match, PROFILE),
        clock=lambda: NOW,
        budget=ScoreBudget(
            target=target,
            max_eur=0.0,
            mode="test",
            triage_reserve_eur=0.0,
            match_reserve_eur=0.0,
            min_score=min_score,
        ),
        rubric_version=RUBRIC_VERSION,
        max_per_company=100,
        cv_sha256=cv_sha256,
    )


def _scripted_match() -> FakeLLM:
    low = reply_with_total(30)
    match = FakeLLM(default=MATCH_REPLY)
    match.script("Engineer 0", low)
    match.script("Engineer 2", low)
    return match


def test_weak_matches_do_not_count_toward_the_target(store: SqliteStore) -> None:
    seed(store, 6)
    match = _scripted_match()
    report = _threshold_run(store, match, target=2, min_score=45).execute()
    # 0 (30) and 2 (30) are scored but do not count; 1 and 3 do, then the run stops.
    assert (report.matched, report.above_threshold, report.target) == (4, 2, 2)
    assert len(match.calls) == 4


def test_a_zero_bar_keeps_the_old_score_the_top_n_behaviour(store: SqliteStore) -> None:
    seed(store, 6)
    report = _threshold_run(store, _scripted_match(), target=2, min_score=0).execute()
    assert (report.matched, report.above_threshold) == (2, 2)


def test_a_shortfall_is_reported_not_padded(store: SqliteStore) -> None:
    seed(store, 2)
    report = _threshold_run(store, _scripted_match(), target=5, min_score=45).execute()
    assert report.above_threshold == 1 and report.target == 5
    assert report.matched == 2  # every candidate was scored, only one cleared the bar


def test_the_call_cap_stops_a_search_for_good_matches(store: SqliteStore) -> None:
    seed(store, 30)
    low = reply_with_total(10)
    match = FakeLLM(default=low)
    report = _threshold_run(store, match, target=2, min_score=45).execute()
    assert len(match.calls) == 2 * 3 and report.above_threshold == 0


def test_scores_record_the_cv_that_produced_them(store: SqliteStore) -> None:
    seed(store, 1)
    _threshold_run(store, FakeLLM(default=MATCH_REPLY), 1, 0, cv_sha256="abc").execute()
    stored = store._conn.execute("SELECT DISTINCT cv_sha256 FROM scores").fetchall()
    assert [row[0] for row in stored] == ["abc"]


def test_a_full_match_run_stores_parts_that_sum_to_the_stored_total(store: SqliteStore) -> None:
    seed(store, 3)
    match = FakeLLM(default=match_reply(skills=30, seniority=20, domain=15, preferences=10))
    build_run(
        store, FakeLLM(default={"relevance": 60, "reason": "fits"}), match, target=10
    ).execute()
    rows = store._conn.execute(
        "SELECT score, verdict, parts, rubric_version FROM scores WHERE stage = 'match'"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        parts = json.loads(row["parts"])
        assert sum(part["points"] for part in parts.values()) == row["score"] == 75
        assert parts["skills"]["reason"] == "skills reason"
        assert row["verdict"] == "strong"
        assert row["rubric_version"] == RUBRIC_VERSION


def test_triage_scores_keep_one_total_and_no_parts(store: SqliteStore) -> None:
    seed(store, 1)
    build_run(
        store, FakeLLM(default={"relevance": 60, "reason": "fits"}), FakeLLM(default=MATCH_REPLY), 5
    ).execute()
    row = store._conn.execute("SELECT parts FROM scores WHERE stage = 'triage'").fetchone()
    assert row["parts"] is None


def test_the_report_counts_candidates_the_per_company_cap_held_back(
    store: SqliteStore,
) -> None:
    """Five postings from one company, a cap of two: three never reach the match step."""
    seed(store, 5)
    report = build_run(
        store,
        FakeLLM(default={"relevance": 60, "reason": "fits"}),
        FakeLLM(default=MATCH_REPLY),
        target=10,
        max_per_company=2,
    ).execute()
    assert (report.matched, report.capped) == (2, 3)
