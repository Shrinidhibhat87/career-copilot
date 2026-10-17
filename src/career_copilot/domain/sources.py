"""Board proposals: what the discovery agent found, and what the user did about it.

Also the company-list import types: a curated watchlist is higher-trust input than a
proposal (the user already did the "worth watching" judgment), so it goes straight into
a resolution ledger rather than a pending/approved/rejected one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from career_copilot.domain.models import SourceTarget


@dataclass(frozen=True, slots=True)
class BoardVisit:
    """One board the crawler visits, as the "added companies" table shows it.

    `origin` is where the board came from: imported, manual, approved, sample_role, or
    config for a target written in config.yaml. `last_visited_at` is None until a run has
    fetched it. `open_roles` is what the board held when it was last counted, if ever."""

    name: str
    source: str
    slug: str
    origin: str
    status: str
    tier: int
    added_at: datetime | None
    open_roles: int | None
    last_visited_at: datetime | None


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class SourceProposal:
    """One verified board the agent thinks is worth crawling.

    `evidence` is required and names what the agent actually saw — the search result
    and the probe's job count — so a reviewer can judge it without re-running anything.
    """

    board_type: str
    slug: str
    company: str
    evidence: str
    confidence: float = 0.0
    job_count: int = 0
    sample_titles: tuple[str, ...] = ()
    status: ProposalStatus = ProposalStatus.PENDING
    id: int | None = None
    run_id: int | None = None
    proposed_at: datetime | None = None
    decided_at: datetime | None = None

    def to_target(self, tier: int = 0, cooldown_hours: int = 24) -> SourceTarget:
        return SourceTarget(
            source=self.board_type,
            identifier=self.slug,
            tier=tier,
            cooldown_hours=cooldown_hours,
        )


class ImportStatus(StrEnum):
    """A company-list row's resolution state. `unresolved` and `blocked` are distinct on
    purpose: a clean 404 across every candidate board says "no public board found"; a
    non-404 error, timeout or login-wall redirect says "there may be one but it couldn't
    be verified" — different signals, both worth surfacing to the user by name."""

    UNRESOLVED = "unresolved"
    RESOLVED = "resolved"
    BLOCKED = "blocked"
    INACTIVE = "inactive"


@dataclass(frozen=True, slots=True)
class CompanyRef:
    """One row from an external company list. `external_id` is stable across re-syncs —
    a Notion page id, or a hash of the normalized name for a source with no natural id."""

    external_id: str
    name: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ImportCompany:
    """A company-list row as held by the store, including its resolution outcome."""

    id: int
    list_source: str
    external_id: str
    name: str
    status: ImportStatus
    board_type: str | None
    slug: str | None
    last_error: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    # When the board was last probed, and how many jobs it held then. Both are `None`
    # for a row never probed (or one synced before they were recorded).
    last_probed_at: datetime | None = None
    open_roles: int | None = None
    # The user chose this board by hand; a sync re-checks it but never re-resolves it.
    pinned: bool = False


_WRAPPERS = {"{": "}", "[": "]", "(": ")"}
_WHITESPACE = re.compile(r"\s+")


def normalize_company_name(raw: str) -> str:
    """Generic text hygiene, not a personalization: trim, collapse whitespace, and strip
    one layer of matching wrapping brackets/braces/parens (`{CERN}` -> `CERN`)."""
    name = raw.strip()
    if len(name) >= 2 and name[0] in _WRAPPERS and name[-1] == _WRAPPERS[name[0]]:
        name = name[1:-1].strip()
    return _WHITESPACE.sub(" ", name)
