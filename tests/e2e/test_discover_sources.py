"""A whole discovery run, scripted turn by turn. No network, no model, no cost.

`FunctionModel` stands in for the model and returns the tool calls a real one would, so
what is asserted here is the run's behaviour: what it verifies before proposing, what it
writes, and what it leaves alone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from career_copilot.adapters.search.fake import FakeSearch
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.discovery.agent import DiscoveryLimits, build_agent
from career_copilot.domain.budget import RunLimits
from career_copilot.domain.filters import FilterConfig
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.domain.preferences import Preferences, bootstrap
from career_copilot.domain.sources import ProposalStatus
from career_copilot.ports import SearchResult
from career_copilot.usecases.discover_sources import (
    DiscoverSourcesRun,
    SearchUnavailable,
    build_run,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 9, 8, tzinfo=UTC)

SEARCH = FakeSearch(
    {
        "berlin": [
            SearchResult("Acme GmbH careers", "https://boards.greenhouse.io/acme", "Berlin"),
            SearchResult("Ghost AG jobs", "https://boards.greenhouse.io/ghost", "Berlin"),
        ]
    }
)


def prefs() -> Preferences:
    return bootstrap(FilterConfig(), notes="", batch_size=6)


def fetcher(url: str) -> Any:
    if "/acme" in url:
        return json.loads((FIXTURES / "greenhouse_board.json").read_text())
    raise RuntimeError("HTTPStatusError")


def scripted(*turns: list[ToolCallPart] | str) -> FunctionModel:
    """Replay one model turn per call, so the run is asserted step by step."""
    remaining = list(turns)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        turn = remaining.pop(0) if remaining else "done"
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=list(turn))

    return FunctionModel(respond)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        yield store


def run_with(model: FunctionModel, store: SqliteStore, **options: Any) -> Any:
    run = DiscoverSourcesRun(
        agent=build_agent(model),
        store=store,
        search=SEARCH,
        preferences=prefs(),
        identity=OwnerIdentity(names=("Marlena Voss",), confidence="confirmed"),
        now=NOW,
        fetcher=fetcher,
        **options,
    )
    return run.execute()


HAPPY_PATH = (
    [ToolCallPart("known_companies", {})],
    [ToolCallPart("web_search", {"query": "data engineer berlin greenhouse board"})],
    [
        ToolCallPart("probe_ats", {"board_type": "greenhouse", "slug": "acme"}),
        ToolCallPart("probe_ats", {"board_type": "greenhouse", "slug": "ghost"}),
    ],
    [ToolCallPart("sample_jobs", {"board_type": "greenhouse", "slug": "acme"})],
    [
        ToolCallPart(
            "propose_source",
            {
                "board_type": "greenhouse",
                "slug": "acme",
                "company": "Acme GmbH",
                "evidence": "search hit 'Acme GmbH careers'; probe returned 2 open jobs",
                "confidence": 0.8,
            },
        )
    ],
    "Found one board: Acme GmbH, hiring in Berlin.",
)


def test_a_run_proposes_only_the_board_it_verified(store: SqliteStore) -> None:
    report = run_with(scripted(*HAPPY_PATH), store, target_proposals=1)

    assert report.found == 1 and report.stored == 1
    proposal = store.list_proposals()[0]
    assert (proposal.board_type, proposal.slug) == ("greenhouse", "acme")
    assert proposal.job_count == 2
    assert "2 open jobs" in proposal.evidence
    assert proposal.status is ProposalStatus.PENDING
    # `ghost` 404s, so it is never proposed however plausible the search result was.
    assert [p.slug for p in store.list_proposals()] == ["acme"]


def test_a_run_never_changes_what_is_crawled(store: SqliteStore) -> None:
    run_with(scripted(*HAPPY_PATH), store, target_proposals=1)
    assert store.list_source_targets() == []


def test_a_dry_run_returns_proposals_without_storing_them(store: SqliteStore) -> None:
    report = run_with(scripted(*HAPPY_PATH), store, target_proposals=1, persist=False)
    assert report.found == 1 and report.stored == 0
    assert store.list_proposals() == []


def test_a_proposal_can_be_linked_to_its_ledger_run(store: SqliteStore) -> None:
    run_id = store.start_run(NOW, kind="source_discovery")
    run_with(scripted(*HAPPY_PATH), store, target_proposals=1, run_id=run_id)
    proposal = store.list_proposals()[0]
    row = store.get_run(run_id)
    assert proposal.run_id == run_id
    assert row is not None and row["kind"] == "source_discovery"


def test_the_summary_and_the_search_count_are_reported(store: SqliteStore) -> None:
    report = run_with(scripted(*HAPPY_PATH), store, target_proposals=1)
    assert report.searches == 1
    assert "Acme" in report.summary


def test_a_board_that_was_never_probed_cannot_be_proposed(store: SqliteStore) -> None:
    script = (
        [
            ToolCallPart(
                "propose_source",
                {
                    "board_type": "greenhouse",
                    "slug": "unverified",
                    "company": "Unverified",
                    "evidence": "it looked plausible",
                },
            )
        ],
        "I could not verify anything.",
    )
    report = run_with(scripted(*script), store)
    assert report.found == 0
    assert store.list_proposals() == []


def test_a_rejected_board_is_not_stored_again(store: SqliteStore) -> None:
    run_with(scripted(*HAPPY_PATH), store, target_proposals=1)
    proposal = store.list_proposals()[0]
    assert proposal.id is not None
    store.reject_proposal(proposal.id, NOW)

    # `known_companies` includes rejected boards, so the refusal happens in the tool: the
    # second run never even reaches the unique index.
    report = run_with(scripted(*HAPPY_PATH), store, target_proposals=1)
    assert report.found == 0 and report.stored == 0
    assert store.list_proposals(ProposalStatus.PENDING) == []
    assert store.list_proposals(ProposalStatus.REJECTED)[0].id == proposal.id


def test_a_request_ceiling_keeps_the_proposals_already_earned(store: SqliteStore) -> None:
    """The model asks for one more turn than it is allowed; the run still writes."""
    endless = (
        *HAPPY_PATH[:-1],
        [ToolCallPart("web_search", {"query": "more berlin companies"})],
        [ToolCallPart("web_search", {"query": "even more berlin companies"})],
    )
    report = run_with(
        scripted(*endless), store, target_proposals=5, limits=DiscoveryLimits(max_requests=6)
    )
    assert report.found == 1 and report.stored == 1
    assert "ceiling" in report.stopped_because


def test_the_wall_clock_ceiling_ends_the_whole_agent_run(store: SqliteStore) -> None:
    class SlowAgent:
        async def run(self, *args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(1)

    run = DiscoverSourcesRun(
        agent=SlowAgent(),  # type: ignore[arg-type]
        store=store,
        search=SEARCH,
        preferences=prefs(),
        identity=OwnerIdentity(),
        now=NOW,
        run_limits=RunLimits(max_seconds=0.001),
    )
    report = run.execute()
    assert report.found == 0
    assert "wall-clock ceiling" in report.stopped_because


def test_a_run_without_a_search_provider_refuses_before_the_model(store: SqliteStore) -> None:
    with pytest.raises(SearchUnavailable, match="TAVILY_API_KEY"):
        build_run(
            scripted("never reached"),
            store,
            None,
            prefs(),
            OwnerIdentity(),
            NOW,
        )


def test_the_search_profile_steers_the_prompt(store: SqliteStore) -> None:
    from career_copilot.domain.search_profile import SearchProfile

    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[0]))
        return ModelResponse(parts=[TextPart("done")])

    profile = SearchProfile(
        "FDE in applied AI", ("forward deployed engineer",), similar_companies=("Taktile",)
    )
    run_with(FunctionModel(respond), store, profile=profile)
    assert "Role archetypes: forward deployed engineer." in seen[0]
    assert "Companies hiring for this kind of role: Taktile." in seen[0]


def test_no_profile_leaves_the_prompt_as_it_was(store: SqliteStore) -> None:
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[0]))
        return ModelResponse(parts=[TextPart("done")])

    run_with(FunctionModel(respond), store)
    assert "Role archetypes" not in seen[0]


def test_a_named_company_makes_the_run_look_for_that_one_board(store: SqliteStore) -> None:
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(str(messages[0]))
        return ModelResponse(parts=[TextPart("done")])

    run_with(FunctionModel(respond), store, company="Helsing", persist=False)
    assert "Helsing" in seen[0]
    assert "only that company" in seen[0]
    assert "role families" not in seen[0].lower()
