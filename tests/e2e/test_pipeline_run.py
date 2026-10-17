"""Pipeline ownership, terminal states, and per-job progress."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.match import MatchAgent
from career_copilot.agents.triage import TriageAgent
from career_copilot.config import Config
from career_copilot.domain.budget import BudgetExceeded
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.profile import Profile
from career_copilot.domain.runs import Progress, RunConfig
from career_copilot.ports import FailureCategory, LLMCallError
from career_copilot.usecases import pipeline
from career_copilot.usecases.pipeline import PipelineRun
from career_copilot.usecases.score import ScoreBudget, ScoreRun
from tests.conftest import make_raw
from tests.match_replies import reply_with_total

NOW = datetime(2026, 9, 6, tzinfo=UTC)
PROFILE = Profile(preferences="Python roles", resume="Built Python systems")
MATCH: dict[str, object] = reply_with_total(88, reasons=["Python"])


def frozen() -> RunConfig:
    return RunConfig(
        Preferences(core_skills=("python",)),
        "triage",
        "match",
        1,
        "initial",
        1.0,
        1,
        0.02,
        0.2,
        256,
        1024,
    )


def begin(store: SqliteStore) -> int:
    return store.begin_run("full", frozen(), NOW, os.getpid(), "birth")


def test_score_progress_fires_once_for_each_paid_job(tmp_path: Path) -> None:
    with SqliteStore(tmp_path / "jobs.db") as store:
        for index in range(2):
            posting = to_posting(
                make_raw(
                    source_job_id=str(index),
                    title=f"Python Engineer {index}",
                    url=f"https://example.test/{index}",
                )
            )
            store.upsert_job(posting, apply_hard_filters(posting), NOW)
        run_id = begin(store)
        updates: list[Progress] = []
        report = ScoreRun(
            store,
            TriageAgent(FakeLLM(default={"relevance": 70, "reason": "fit"}), PROFILE.preferences),
            MatchAgent(FakeLLM(default=MATCH), PROFILE),
            lambda: NOW,
            ScoreBudget(1, 1.0, "initial", 0.02, 0.2),
            run_id=run_id,
            progress=updates.append,
        ).execute()
        assert (report.triaged, report.matched) == (2, 1)
        assert [item.completed for item in updates] == [1, 2, 3]
        assert all(item.stage == "score" for item in updates)


class Stage:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error

    def execute(self) -> None:
        if self.error is not None:
            raise self.error


@pytest.mark.parametrize(
    ("error", "state", "category"),
    [
        (RuntimeError("boom"), "failed", "internal"),
        (BudgetExceeded("ceiling reached"), "aborted", "budget"),
        (
            LLMCallError("no credit", category=FailureCategory.CREDIT, calls=1),
            "failed",
            "credit",
        ),
    ],
)
def test_pipeline_finalizes_every_failure_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
    state: str,
    category: str,
) -> None:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    with SqliteStore(config.db_path) as store:
        run_id = begin(store)

        def forbidden(*args: Any, **kwargs: Any) -> int:
            raise AssertionError("PipelineRun must not create a ledger row")

        monkeypatch.setattr(store, "begin_run", forbidden)
        monkeypatch.setattr(pipeline, "build_discover_run", lambda *args, **kwargs: Stage(error))
        PipelineRun(store, config, run_id).execute()
        row = store.get_run(run_id)
        assert row is not None
        assert (row["state"], row["failure_category"]) == (state, category)
        assert len(store.list_runs()) == 1


def test_cancelled_pipeline_is_finalized_by_its_worker(tmp_path: Path) -> None:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    with SqliteStore(config.db_path) as store:
        run_id = begin(store)
        cancel = Event()
        cancel.set()
        PipelineRun(store, config, run_id, cancel=cancel).execute()
        row = store.get_run(run_id)
        assert row is not None
        assert (row["state"], row["failure_category"]) == ("aborted", "shutdown")


def test_one_malformed_reply_costs_its_job_and_not_the_run(tmp_path: Path) -> None:
    """A schema failure is a bad job, not a bad run — in the worker as on the CLI.

    The browser path is the expensive one: aborting the whole run on the third of sixty
    jobs would throw away the spend on the first two and every job after it.
    """
    with SqliteStore(tmp_path / "jobs.db") as store:
        for index in range(2):
            posting = to_posting(
                make_raw(
                    source_job_id=str(index),
                    title=f"Python Engineer {index}",
                    url=f"https://example.test/{index}",
                )
            )
            store.upsert_job(posting, apply_hard_filters(posting), NOW)
        run_id = begin(store)
        triage = FakeLLM(
            responses={"Python Engineer 0": {"relevance": "not a number", "reason": "fit"}},
            default={"relevance": 70, "reason": "fit"},
        )
        report = ScoreRun(
            store,
            TriageAgent(triage, PROFILE.preferences),
            MatchAgent(FakeLLM(default=MATCH), PROFILE),
            lambda: NOW,
            ScoreBudget(1, 1.0, "initial", 0.02, 0.2),
            run_id=run_id,
            preferences_hash="hash",
        ).execute()
        assert (report.triaged, report.matched, report.failed) == (1, 1, 1)
        row = store.get_run(run_id)
        assert row is not None
        assert row["state"] == "running"  # the caller finalizes; the run did not fail
        assert row["failed"] == 1
