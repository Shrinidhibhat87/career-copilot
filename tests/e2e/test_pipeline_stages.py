from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.models import DiscoverReport, ScoreReport, SourceRunReport
from career_copilot.usecases import pipeline
from career_copilot.usecases.pipeline import PipelineRun
from tests.e2e.test_pipeline_run import begin


class Returns:
    """A stage builder whose run returns a fixed report, or does nothing."""

    def __init__(self, report: object = None) -> None:
        self._report = report

    def execute(self) -> object:
        return self._report


def stub_stages(monkeypatch: pytest.MonkeyPatch, *, score_fails: bool = False) -> None:
    discovered = DiscoverReport(
        run_id=1,
        fetched=10,
        inserted=4,
        qualified=3,
        rejected=5,
        skipped_targets=("greenhouse:n26 (cooldown)",),
        source_runs=(SourceRunReport("greenhouse", "helsing", ok=True, fetched=10),),
    )
    scored = ScoreReport(
        run_id=1, candidates=3, matched=3, above_threshold=2, target=5, failed=0, capped=0
    )

    class Failing:
        def execute(self) -> object:
            raise RuntimeError("model gone")

    monkeypatch.setattr(pipeline, "build_discover_run", lambda *a, **k: Returns(discovered))
    monkeypatch.setattr(
        pipeline, "refresh_profile", lambda config, store, frozen, run_id=None: frozen
    )
    monkeypatch.setattr(
        pipeline, "build_score_run", lambda *a, **k: Failing() if score_fails else Returns(scored)
    )
    monkeypatch.setattr(pipeline, "build_digest_run", lambda *a, **k: Returns())


def test_a_run_records_four_stages_in_order_and_a_funnel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_stages(monkeypatch)
    config = Config(db_path=str(tmp_path / "jobs.db"))
    with SqliteStore(config.db_path) as store:
        run_id = begin(store)
        PipelineRun(store, config, run_id).execute()
        row: dict[str, Any] | None = store.get_run(run_id)
    assert row is not None and row["state"] == "succeeded"
    assert [s["stage"] for s in row["stages"]] == ["discover", "profile", "score", "digest"]
    assert all(s["endedAt"] is not None for s in row["stages"])
    funnel = row["funnel"]
    assert funnel["discover"]["searched"] == 1
    assert funnel["discover"]["skipped"] == [{"target": "greenhouse:n26", "reason": "cooldown"}]
    assert funnel["score"]["above"] == 2
    assert funnel["score"]["stoppedBy"] == "out of candidates"


def test_a_stage_that_raises_stays_open_and_the_run_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_stages(monkeypatch, score_fails=True)
    config = Config(db_path=str(tmp_path / "jobs.db"))
    with SqliteStore(config.db_path) as store:
        run_id = begin(store)
        PipelineRun(store, config, run_id).execute()
        row = store.get_run(run_id)
    assert row is not None and row["state"] == "failed"
    stages = {s["stage"]: s for s in row["stages"]}
    assert stages["discover"]["endedAt"] is not None
    assert stages["score"]["endedAt"] is None
    assert "digest" not in stages
    assert row["funnel"] is None


def test_the_curator_recording_carries_the_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.e2e.test_pipeline_run import frozen

    seen: list[tuple[str, int | None]] = []

    def fake_recording(config: Config, role: object, agent: str, run_id: int | None = None) -> None:
        seen.append((agent, run_id))

    monkeypatch.setattr(pipeline, "agent_recording", fake_recording)
    config = Config(db_path=str(tmp_path / "jobs.db"))
    with SqliteStore(config.db_path) as store:
        pipeline.build_ranker(config, store, frozen(), 7)
    assert seen == [("curator", 7)]
