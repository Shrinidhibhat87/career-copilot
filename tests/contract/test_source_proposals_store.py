"""Proposals are data; approval is the only path into the crawled target list."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.sources import ProposalStatus, SourceProposal

NOW = datetime(2026, 9, 8, 12, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        yield store


def proposal(slug: str = "acme", board: str = "greenhouse") -> SourceProposal:
    return SourceProposal(
        board_type=board,
        slug=slug,
        company="Acme GmbH",
        evidence="search hit 'acme careers greenhouse'; probe returned 42 jobs",
        confidence=0.8,
        job_count=42,
        sample_titles=("Data Engineer", "ML Engineer"),
    )


def test_a_proposal_round_trips_with_its_evidence(store: SqliteStore) -> None:
    proposal_id = store.insert_proposal(proposal(), NOW)
    assert proposal_id is not None
    stored = store.list_proposals()[0]
    assert stored.slug == "acme"
    assert stored.sample_titles == ("Data Engineer", "ML Engineer")
    assert "42 jobs" in stored.evidence
    assert stored.status is ProposalStatus.PENDING


def test_the_same_board_cannot_be_proposed_twice(store: SqliteStore) -> None:
    assert store.insert_proposal(proposal(), NOW) is not None
    assert store.insert_proposal(proposal(), NOW) is None


def test_the_same_slug_on_another_board_is_a_different_proposal(store: SqliteStore) -> None:
    assert store.insert_proposal(proposal(), NOW) is not None
    assert store.insert_proposal(proposal(board="lever"), NOW) is not None


def test_nothing_is_crawled_until_a_proposal_is_approved(store: SqliteStore) -> None:
    store.insert_proposal(proposal(), NOW)
    assert store.list_source_targets() == []


def test_approval_promotes_the_proposal_into_the_target_list(store: SqliteStore) -> None:
    proposal_id = store.insert_proposal(proposal(), NOW)
    assert proposal_id is not None
    target = store.approve_proposal(proposal_id, NOW)
    assert target is not None
    assert (target.source, target.identifier) == ("greenhouse", "acme")
    assert [t.identifier for t in store.list_source_targets()] == ["acme"]
    assert store.list_proposals(ProposalStatus.APPROVED)[0].id == proposal_id


def test_approving_twice_changes_nothing(store: SqliteStore) -> None:
    proposal_id = store.insert_proposal(proposal(), NOW)
    assert proposal_id is not None
    store.approve_proposal(proposal_id, NOW)
    assert store.approve_proposal(proposal_id, NOW) is None
    assert len(store.list_source_targets()) == 1


def test_a_rejected_proposal_stays_so_the_agent_cannot_repeat_it(store: SqliteStore) -> None:
    proposal_id = store.insert_proposal(proposal(), NOW)
    assert proposal_id is not None
    assert store.reject_proposal(proposal_id, NOW) is True
    assert store.list_proposals() == []
    assert ("greenhouse", "acme") in store.known_slugs()
    assert store.insert_proposal(proposal(), NOW) is None


def test_a_rejected_proposal_cannot_then_be_approved(store: SqliteStore) -> None:
    proposal_id = store.insert_proposal(proposal(), NOW)
    assert proposal_id is not None
    store.reject_proposal(proposal_id, NOW)
    assert store.approve_proposal(proposal_id, NOW) is None
    assert store.list_source_targets() == []
