"""Each discovery tool alone. No network: probes read recorded board payloads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from career_copilot.adapters.search.fake import FakeSearch
from career_copilot.agents.discovery.tools import (
    DiscoveryDeps,
    known_companies,
    probe_ats,
    propose_source,
    sample_jobs,
    web_search,
)
from career_copilot.domain.budget import CostMeter, RunLimits
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.ports import SearchResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
NOW = datetime(2026, 9, 8, tzinfo=UTC)

IDENTITY = OwnerIdentity(
    names=("Marlena Voss",), emails=("someone@example.com",), confidence="confirmed"
)


def board_fetcher(available: dict[str, str]) -> Any:
    """Serve recorded payloads by slug; anything else raises, exactly as a 404 would."""

    def fetch(url: str) -> Any:
        for slug, fixture in available.items():
            if f"/{slug}" in url:
                return json.loads((FIXTURES / fixture).read_text())
        raise RuntimeError("HTTPStatusError")

    return fetch


def deps(**overrides: Any) -> DiscoveryDeps:
    base: dict[str, Any] = dict(
        search=FakeSearch(
            {"berlin": [SearchResult("Acme careers", "https://acme.io/jobs", "hiring")]}
        ),
        known=set(),
        companies=frozenset({"acme gmbh"}),
        identity=IDENTITY,
        meter=CostMeter(RunLimits()),
        now=NOW,
        fetcher=board_fetcher({"acme": "greenhouse_board.json"}),
    )
    base.update(overrides)
    return DiscoveryDeps(**base)


def probed(dependencies: DiscoveryDeps) -> DiscoveryDeps:
    probe_ats(dependencies, "greenhouse", "acme")
    return dependencies


# --- web_search ---------------------------------------------------------------


def test_a_search_returns_the_hits_it_was_given() -> None:
    assert "Acme careers" in web_search(deps(), "data engineer berlin greenhouse")


def test_a_query_naming_the_resume_owner_is_refused() -> None:
    dependencies = deps()
    answer = web_search(dependencies, "Marlena Voss resume berlin")
    assert answer.startswith("refused")
    assert dependencies.searches == 0


def test_the_email_and_phone_are_guarded_too() -> None:
    assert web_search(deps(), "someone@example.com jobs").startswith("refused")


def test_the_search_cap_refuses_rather_than_raises() -> None:
    dependencies = deps(max_searches=1)
    web_search(dependencies, "berlin data engineer")
    answer = web_search(dependencies, "berlin ml engineer")
    assert "cap" in answer and dependencies.searches == 1


def test_a_spent_euro_ceiling_stops_searching_and_says_so() -> None:
    dependencies = deps(max_eur=0.005)
    answer = web_search(dependencies, "berlin data engineer")
    assert answer.startswith("refused")
    assert "reserves" in dependencies.stopped_because


# --- probe_ats ----------------------------------------------------------------


def test_a_real_board_reports_its_job_count() -> None:
    dependencies = deps()
    assert "2 open jobs" in probe_ats(dependencies, "greenhouse", "acme")
    assert dependencies.probes[("greenhouse", "acme")].exists


def test_a_missing_board_is_a_finding_not_an_exception() -> None:
    dependencies = deps()
    assert "no board" in probe_ats(dependencies, "greenhouse", "not-a-company")
    assert not dependencies.probes[("greenhouse", "not-a-company")].exists


def test_an_unknown_board_type_names_the_ones_that_exist() -> None:
    answer = probe_ats(deps(), "workday", "acme")
    assert "greenhouse" in answer and "lever" in answer


def test_a_slug_with_stray_slashes_is_still_probed() -> None:
    assert "2 open jobs" in probe_ats(deps(), "greenhouse", "/acme/")


# --- sample_jobs --------------------------------------------------------------


def test_sampling_shows_titles_with_their_locations() -> None:
    answer = sample_jobs(probed(deps()), "greenhouse", "acme")
    assert "Forward Deployed Engineer" in answer and "Berlin" in answer


def test_sampling_an_unprobed_board_says_to_probe_it_first() -> None:
    assert sample_jobs(deps(), "greenhouse", "acme") == "probe_ats this board first"


# --- known_companies ----------------------------------------------------------


def test_known_lists_both_decided_boards_and_stored_companies() -> None:
    answer = known_companies(deps(known={("greenhouse", "acme"), ("lever", "beta")}))
    assert "greenhouse/acme" in answer and "lever/beta" in answer and "acme gmbh" in answer


# --- propose_source -----------------------------------------------------------


def test_a_proposal_needs_a_successful_probe_in_the_same_run() -> None:
    dependencies = deps()
    answer = propose_source(dependencies, "greenhouse", "acme", "Acme", "looks good")
    assert answer.startswith("refused")
    assert dependencies.proposals == []


def test_a_verified_board_is_proposed_with_the_job_count_it_probed() -> None:
    dependencies = probed(deps())
    propose_source(dependencies, "greenhouse", "acme", "Acme GmbH", "search hit; 2 open jobs", 0.9)
    proposal = dependencies.proposals[0]
    assert (proposal.slug, proposal.job_count) == ("acme", 2)
    assert proposal.sample_titles[0].startswith("Forward Deployed")


def test_evidence_is_required() -> None:
    dependencies = probed(deps())
    assert propose_source(dependencies, "greenhouse", "acme", "Acme", "  ").startswith("refused")


def test_a_board_already_known_is_refused_even_after_probing() -> None:
    dependencies = probed(deps(known={("greenhouse", "acme")}))
    answer = propose_source(dependencies, "greenhouse", "acme", "Acme", "2 open jobs")
    assert "already known" in answer


def test_the_same_board_cannot_be_proposed_twice_in_one_run() -> None:
    dependencies = probed(deps())
    propose_source(dependencies, "greenhouse", "acme", "Acme", "2 open jobs")
    answer = propose_source(dependencies, "greenhouse", "acme", "Acme", "2 open jobs")
    assert "already proposed" in answer
    assert len(dependencies.proposals) == 1


def test_reaching_the_target_tells_the_model_to_stop() -> None:
    dependencies = probed(deps(target_proposals=1))
    answer = propose_source(dependencies, "greenhouse", "acme", "Acme", "2 open jobs")
    assert "stop now" in answer
