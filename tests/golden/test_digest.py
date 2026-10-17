from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.notify.markdown import MarkdownNotifier
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.match import MatchAgent
from career_copilot.agents.triage import TriageAgent
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.profile import Profile
from career_copilot.domain.sources import CompanyRef, ImportStatus
from career_copilot.usecases.digest import DigestRun
from career_copilot.usecases.score import ScoreBudget, ScoreRun
from tests.conftest import make_raw
from tests.match_replies import reply_with_total

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
PROFILE = Profile(preferences="Forward-deployed AI roles in Berlin.", resume="Built LangGraph POC")


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


def scored_store(store: SqliteStore) -> None:
    for index, title in enumerate(["Forward Deployed Engineer", "AI Engineer"]):
        posting = to_posting(
            make_raw(
                source_job_id=str(1000 + index),
                title=title,
                url=f"https://boards.greenhouse.io/acme/jobs/{1000 + index}",
            )
        )
        store.upsert_job(posting, apply_hard_filters(posting), NOW)
    match = FakeLLM(default=reply_with_total(52, reasons=["some overlap"], gaps=["no Kubernetes"]))
    match.script(
        "Forward Deployed Engineer",
        reply_with_total(
            88,
            reasons=["agentic AI production ownership"],
            gaps=["no Terraform"],
            warnings=["Munich, not Berlin"],
        ),
    )
    ScoreRun(
        store=store,
        triage=TriageAgent(
            FakeLLM(default={"relevance": 70, "reason": "fits"}), PROFILE.preferences
        ),
        match=MatchAgent(match, PROFILE),
        clock=lambda: NOW,
        budget=ScoreBudget(target=10, max_eur=0.0, mode="test"),
    ).execute()


def test_digest_lists_matches_highest_first(store: SqliteStore, tmp_path: Path) -> None:
    scored_store(store)
    out = tmp_path / "digest.md"
    body = DigestRun(store, MarkdownNotifier(out), clock=lambda: NOW).execute()
    assert body.index("Forward Deployed Engineer") < body.index("AI Engineer")
    assert "88" in body
    assert out.read_text() == body


def test_digest_shows_reasons_gaps_warnings_and_evidence(
    store: SqliteStore, tmp_path: Path
) -> None:
    scored_store(store)
    body = DigestRun(store, MarkdownNotifier(tmp_path / "d.md"), clock=lambda: NOW).execute()
    assert "agentic AI production ownership" in body
    assert "no Terraform" in body
    assert "Munich, not Berlin" in body
    assert "Built LangGraph POC" in body


def test_digest_lists_manual_review_jobs(store: SqliteStore, tmp_path: Path) -> None:
    posting = to_posting(
        make_raw(
            source_job_id="9999",
            description="Permanent Python role. You must be eligible to work in Germany.",
            url="https://boards.greenhouse.io/acme/jobs/9999",
        )
    )
    store.upsert_job(posting, apply_hard_filters(posting), NOW)
    body = DigestRun(store, MarkdownNotifier(tmp_path / "d.md"), clock=lambda: NOW).execute()
    assert "Manual review" in body


def test_an_empty_digest_says_so_rather_than_rendering_nothing(
    store: SqliteStore, tmp_path: Path
) -> None:
    body = DigestRun(store, MarkdownNotifier(tmp_path / "d.md"), clock=lambda: NOW).execute()
    assert "No scored jobs" in body


def test_digest_shows_current_statistics_and_latest_source_failures(
    store: SqliteStore, tmp_path: Path
) -> None:
    run_id = store.start_run(NOW)
    store.record_source_run(run_id, "greenhouse", "broken", False, 0, "timeout", NOW)
    store.finish_run(run_id, NOW, "failed=1")
    body = DigestRun(store, MarkdownNotifier(tmp_path / "d.md"), clock=lambda: NOW).execute()
    assert "## Statistics" in body
    assert "Active jobs: 0" in body
    assert "## Source failures" in body
    assert "greenhouse:broken — timeout" in body


def test_digest_shows_watchlist_coverage_distinguishing_unresolved_from_blocked(
    store: SqliteStore, tmp_path: Path
) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Ghost Co"), NOW)
    store.record_resolution("csv", "1", ImportStatus.UNRESOLVED, None, None, None, NOW)
    store.upsert_import_company("csv", CompanyRef("2", "Blocked Co"), NOW)
    store.record_resolution("csv", "2", ImportStatus.BLOCKED, None, None, "timed out", NOW)
    body = DigestRun(store, MarkdownNotifier(tmp_path / "d.md"), clock=lambda: NOW).execute()
    assert "## Watchlist coverage" in body
    assert "Ghost Co: no public board found" in body
    assert "Blocked Co: blocked — timed out" in body


def test_digest_omits_watchlist_coverage_when_everything_resolved(
    store: SqliteStore, tmp_path: Path
) -> None:
    store.upsert_import_company("csv", CompanyRef("1", "Acme Corp"), NOW)
    store.record_resolution("csv", "1", ImportStatus.RESOLVED, "greenhouse", "acme", None, NOW)
    body = DigestRun(store, MarkdownNotifier(tmp_path / "d.md"), clock=lambda: NOW).execute()
    assert "## Watchlist coverage" not in body


def test_the_digest_is_byte_identical_across_runs(store: SqliteStore, tmp_path: Path) -> None:
    scored_store(store)
    first = DigestRun(store, MarkdownNotifier(tmp_path / "a.md"), clock=lambda: NOW).execute()
    second = DigestRun(store, MarkdownNotifier(tmp_path / "b.md"), clock=lambda: NOW).execute()
    assert first == second


def test_ranked_digest_caps_each_company(store: SqliteStore, tmp_path: Path) -> None:
    for index in range(4):
        posting = to_posting(
            make_raw(
                source_job_id=f"cap-{index}",
                title=f"Python Engineer {index}",
                url=f"https://example.com/cap-{index}",
            )
        )
        store.upsert_job(posting, apply_hard_filters(posting), NOW)
    ScoreRun(
        store=store,
        triage=TriageAgent(FakeLLM(default={"relevance": 70, "reason": "fits"}), "prefs"),
        match=MatchAgent(
            FakeLLM(default=reply_with_total(80, reasons=["fit"])),
            PROFILE,
        ),
        clock=lambda: NOW,
        budget=ScoreBudget(target=10, max_eur=0, mode="test"),
        max_per_company=10,
    ).execute()
    body = DigestRun(
        store,
        MarkdownNotifier(tmp_path / "cap.md"),
        clock=lambda: NOW,
        max_per_company=2,
    ).execute()
    assert body.count("### 80 — Python Engineer") == 2


def test_the_notifier_creates_parent_directories(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "digest.md"
    MarkdownNotifier(target).deliver("Digest", "# hello")
    assert target.read_text() == "# hello"
