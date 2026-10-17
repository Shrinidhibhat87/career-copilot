"""Rank → curate → match: the embedding path, scripted curator, and every fallback."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from career_copilot.adapters.embed.fake import FakeEmbedder
from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.curator.agent import build_curator
from career_copilot.agents.curator.tools import CuratorDeps, search_pool, shortlist
from career_copilot.agents.match import MatchAgent
from career_copilot.agents.triage import TriageAgent
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import StoredJob
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.profile import Profile
from career_copilot.domain.sample_roles import SampleRole
from career_copilot.usecases.curate import EmbeddingRanker
from career_copilot.usecases.score import ScoreBudget, ScoreRun
from tests.conftest import make_raw
from tests.match_replies import reply_with_total

NOW = datetime(2026, 10, 7, tzinfo=UTC)
TITLES = {
    "fde": "Forward Deployed Engineer decision engines",
    "data": "Data Engineer Spark pipelines",
    "sales": "Solutions Engineer enterprise onboarding",
    "ml": "Machine Learning Engineer ranking",
}
MATCH: dict[str, object] = reply_with_total(80, reasons=["fit"])


def seeded() -> tuple[SqliteStore, dict[str, int]]:
    store = SqliteStore()
    for index, (key, title) in enumerate(TITLES.items()):
        posting = to_posting(
            make_raw(
                source_job_id=str(index),
                company=f"Company {key}",
                title=title,
                url=f"https://boards.greenhouse.io/x/jobs/{index}",
                description=f"Permanent role building agentic AI with Python. {title}.",
            )
        )
        store.upsert_job(posting, apply_hard_filters(posting), NOW)
    ids = {job.posting.title.split()[0].lower(): job.id for job in store.all_active_jobs()}
    return store, {key: ids[TITLES[key].split()[0].lower()] for key in TITLES}


def add_sample(store: SqliteStore) -> None:
    store.add_sample_role(
        SampleRole(
            0,
            "ashby",
            "t",
            "1",
            "Taktile",
            "Forward Deployed Engineer",
            "Forward deployed engineer building decision engines.",
            None,
            NOW,
        ),
        NOW,
    )


def candidates(store: SqliteStore) -> Sequence[StoredJob]:
    return store.jobs_needing_score("match", 1)


def ranker(store: SqliteStore, curator: Any = None, embedder: Any = None) -> EmbeddingRanker:
    return EmbeddingRanker(
        store=store,
        embedder=embedder or FakeEmbedder,
        clock=lambda: NOW,
        curator=curator,
    )


def scripted(*turns: list[ToolCallPart] | str) -> Any:
    remaining = list(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = remaining.pop(0) if remaining else "done"
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=list(turn))

    return lambda: build_curator(FunctionModel(respond))


def test_no_references_means_no_ranking() -> None:
    store, _ = seeded()
    assert ranker(store)(candidates(store), 2) is None


def test_the_sample_role_ranks_its_lookalike_first() -> None:
    store, ids = seeded()
    add_sample(store)
    order = ranker(store)(candidates(store), 2)
    assert order is not None and order[0].id == ids["fde"]
    assert len(order) == len(TITLES)


def test_the_curators_shortlist_goes_first_then_the_embedding_order() -> None:
    store, ids = seeded()
    add_sample(store)
    curator = scripted(
        [ToolCallPart("search_pool", {"query": "machine learning", "k": 3})],
        [ToolCallPart("shortlist", {"job_id": ids["ml"], "reason": "ranking work"})],
        "Picked one.",
    )
    run = ranker(store, curator)
    order = run(candidates(store), 2)
    assert order is not None
    assert [job.id for job in order[:2]] == [ids["ml"], ids["fde"]]
    assert run.shortlisted == 1


def test_a_broken_curator_keeps_its_picks_and_the_embedding_order() -> None:
    store, ids = seeded()
    add_sample(store)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) > 1:
            raise RuntimeError("model crashed")
        return ModelResponse(
            parts=[ToolCallPart("shortlist", {"job_id": ids["data"], "reason": "x"})]
        )

    order = ranker(store, lambda: build_curator(FunctionModel(respond)))(candidates(store), 2)
    assert order is not None
    assert [job.id for job in order[:2]] == [ids["data"], ids["fde"]]


def test_a_broken_embedder_falls_back_to_triage() -> None:
    store, _ = seeded()
    add_sample(store)

    def broken() -> Any:
        raise OSError("model download failed")

    assert ranker(store, embedder=broken)(candidates(store), 2) is None


def test_shortlist_refuses_unknown_duplicate_and_overflow() -> None:
    store, ids = seeded()
    pool = list(candidates(store))
    deps = CuratorDeps(pool, {j.id: [1.0] for j in pool}, lambda _: [1.0], limit=1)
    assert shortlist(deps, 999, "x").startswith("refused")
    assert shortlist(deps, ids["fde"], "x").startswith("shortlisted")
    assert "already" in shortlist(deps, ids["fde"], "x")
    assert shortlist(deps, ids["ml"], "x").startswith("refused: the shortlist is full")
    assert "shortlisted" in search_pool(deps, "anything", 50)


def score_run(store: SqliteStore, triage: FakeLLM, rank: Any, target: int) -> ScoreRun:
    profile = Profile(preferences="FDE", resume="cv")
    return ScoreRun(
        store=store,
        triage=TriageAgent(triage, profile.preferences),
        match=MatchAgent(FakeLLM(default=MATCH), profile),
        clock=lambda: NOW,
        budget=ScoreBudget(target=target, max_eur=0.0, mode="test", min_score=90),
        max_per_company=10,
        ranker=rank,
    )


def test_the_embedding_path_makes_no_triage_calls_and_caps_matches_at_twice_target() -> None:
    store, ids = seeded()
    add_sample(store)
    triage = FakeLLM(default={"relevance": 50, "reason": "x"})
    # min_score 90 is never cleared, so the run goes to its call cap.
    report = score_run(store, triage, ranker(store), target=1).execute()
    assert triage.calls == []
    assert report.matched == 2
    rows = store._conn.execute("SELECT job_id FROM scores WHERE stage = 'match'").fetchall()
    assert ids["fde"] in {row["job_id"] for row in rows}


def test_a_ranker_that_cannot_rank_falls_back_to_triage() -> None:
    store, _ = seeded()
    triage = FakeLLM(default={"relevance": 50, "reason": "x"})
    report = score_run(store, triage, ranker(store), target=1).execute()
    assert len(triage.calls) == len(TITLES)
    assert report.triaged == len(TITLES)


@pytest.mark.parametrize("target", [1, 3])
def test_embedding_path_summary_names_the_ranker(target: int) -> None:
    store, _ = seeded()
    add_sample(store)
    run = score_run(store, FakeLLM(default={"relevance": 1}), ranker(store), target)
    report = run.execute()
    row = store.get_run(report.run_id)
    assert row is not None and "ranker=embedding" in row["summary"]


def first_id(order: list[StoredJob] | None) -> int:
    assert order
    return order[0].id


def test_the_cv_pulls_its_lookalike_up_the_order() -> None:
    store, ids = seeded()
    add_sample(store)  # a sample role that looks like the fde posting
    cv = "Spark pipelines data engineer with years of Spark pipelines experience"
    with_cv = EmbeddingRanker(
        store=store, embedder=FakeEmbedder, clock=lambda: NOW, cv_text=cv, cv_weight=0.9
    )

    assert first_id(ranker(store)(candidates(store), 4)) == ids["fde"]
    assert first_id(with_cv(candidates(store), 4)) == ids["data"]


def test_no_cv_text_leaves_the_order_alone() -> None:
    store, _ = seeded()
    add_sample(store)
    blank = EmbeddingRanker(
        store=store, embedder=FakeEmbedder, clock=lambda: NOW, cv_text="", cv_weight=0.9
    )
    plain = ranker(store)

    assert blank(candidates(store), 4) == plain(candidates(store), 4)
