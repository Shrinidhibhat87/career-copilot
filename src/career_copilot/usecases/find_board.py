"""Type one company name, see which job board it is on, then add it.

The same slug guessing a company-list sync uses, aimed at one name. Finding changes
nothing; only `add_board` writes, and it checks the board again first, so a wrong guess
can never enter the crawl.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from career_copilot.adapters.sources.ats import SOURCE_TYPES, Fetcher, http_fetcher
from career_copilot.domain.company_resolution import probe_board, resolve_company, slug_candidates
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.sources import (
    CompanyRef,
    ImportCompany,
    ImportStatus,
    normalize_company_name,
)
from career_copilot.usecases.import_sources import DEFAULT_COOLDOWN_HOURS, DEFAULT_TIER


class AddBoardError(ValueError):
    """The board could not be added. The message is written for the person who asked."""


@dataclass(frozen=True, slots=True)
class BoardHit:
    board_type: str
    slug: str
    open_roles: int
    # Set only by `add_board`: nothing was written, the crawl already visits this board.
    already_listed: bool = False
    # Set only by `add_board`: the name of the list entry this board was given to.
    filled_list_entry: str | None = None


@dataclass(frozen=True, slots=True)
class BoardLookup:
    """`hit` is None when nothing was found. `blocked_reason` is set when a board may
    exist but could not be checked (a rate limit, a timeout), which is not the same as
    "no board": the answer is to try again, not to search the web."""

    name: str
    hit: BoardHit | None
    tried: tuple[str, ...]
    blocked_reason: str | None = None


class ManualTargets(Protocol):
    def list_source_targets(self, active_only: bool = True) -> Sequence[SourceTarget]: ...

    def list_import_companies(
        self, list_source: str | None = None, statuses: Sequence[ImportStatus] | None = None
    ) -> Sequence[ImportCompany]: ...

    def record_manual_resolution(
        self,
        list_source: str,
        external_id: str,
        board_type: str,
        slug: str,
        open_roles: int | None,
        now: datetime,
    ) -> None: ...

    def insert_imported_target(
        self, target: SourceTarget, now: datetime, external_id: str
    ) -> None: ...

    def add_manual_target(
        self,
        source: str,
        slug: str,
        now: datetime,
        name: str | None = None,
        open_roles: int | None = None,
    ) -> None: ...


def find_board(name: str, fetch: Fetcher | None = None, url: str | None = None) -> BoardLookup:
    name = normalize_company_name(name)
    if not name:
        raise ValueError("a company name is required")
    resolved = resolve_company(
        CompanyRef(f"lookup:{name.lower()}", name, url), fetch or http_fetcher()
    )
    if resolved.status is ImportStatus.RESOLVED and resolved.board_type and resolved.slug:
        hit = BoardHit(resolved.board_type, resolved.slug, resolved.open_roles or 0)
        return BoardLookup(name, hit, (resolved.slug,))
    blocked = resolved.last_error if resolved.status is ImportStatus.BLOCKED else None
    return BoardLookup(name, None, slug_candidates(name, url), blocked)


def add_board(
    store: ManualTargets,
    board_type: str,
    slug: str,
    now: datetime,
    fetch: Fetcher | None = None,
    name: str | None = None,
    config_targets: Collection[tuple[str, str]] = (),
) -> BoardHit:
    """Verify the board against the real vendor, then add it to the crawl.

    A board the crawl already visits (in the store, or written in config.yaml) changes
    nothing and is reported as already listed. A board typed for a company the list could
    not find fills in that list entry, so the company is not counted twice."""
    board_type = board_type.strip().lower()
    slug = slug.strip().strip("/")
    if board_type not in SOURCE_TYPES or not slug:
        raise AddBoardError(f"Vendors I know: {', '.join(SOURCE_TYPES)}.")
    listed = {(t.source, t.identifier) for t in store.list_source_targets(True)}
    if (board_type, slug) in listed or (board_type, slug) in config_targets:
        return BoardHit(board_type, slug, 0, already_listed=True)
    probe = probe_board(fetch or http_fetcher(), board_type, slug)
    if not probe.exists:
        if probe.blocked_reason:
            raise AddBoardError(
                f"Could not check {board_type}/{slug} right now ({probe.blocked_reason}). "
                "Nothing was changed; try again later."
            )
        raise AddBoardError(f"{board_type} has no board called '{slug}'. Nothing was changed.")
    open_roles = probe.open_roles or 0
    typed = normalize_company_name(name or "")
    unfound = _unfound_entry(store, typed)
    if unfound is not None:
        store.record_manual_resolution(
            unfound.list_source, unfound.external_id, board_type, slug, probe.open_roles, now
        )
        store.insert_imported_target(
            SourceTarget(board_type, slug, DEFAULT_TIER, DEFAULT_COOLDOWN_HOURS),
            now,
            unfound.external_id,
        )
        return BoardHit(board_type, slug, open_roles, filled_list_entry=unfound.name)
    store.add_manual_target(board_type, slug, now, typed or None, open_roles)
    return BoardHit(board_type, slug, open_roles)


def _unfound_entry(store: ManualTargets, typed: str) -> ImportCompany | None:
    """The list entry of this name that no board was found for, if there is one."""
    if not typed:
        return None
    wanted = typed.casefold()
    for company in store.list_import_companies(
        None, (ImportStatus.UNRESOLVED, ImportStatus.BLOCKED)
    ):
        if normalize_company_name(company.name).casefold() == wanted:
            return company
    return None
