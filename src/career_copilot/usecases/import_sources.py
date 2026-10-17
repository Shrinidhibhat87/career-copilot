"""Company-list sync: fetch a curated watchlist, resolve each name to a verified ATS
board. Sibling to `discover_sources.py`, but deterministic end to end — no agent, no
LLM, no search cost. Importing a list the user already curated is the approval, so a
resolved company goes straight into the active crawl.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from career_copilot.adapters.sources.ats import Fetcher, http_fetcher
from career_copilot.domain.company_resolution import (
    ResolvedCompany,
    probe_board,
    resolve_company,
)
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.sources import CompanyRef, ImportCompany, ImportStatus
from career_copilot.ports import CompanyListProvider, CompanyListStore

# A company probed more recently than this is not probed again, so re-syncing a list
# does not re-ask a hundred hosts a question whose answer has not changed.
RECHECK_AFTER = timedelta(hours=24)
# Probes are network-bound and independent; the store is not thread-safe, so every
# write below stays on the calling thread.
PROBE_WORKERS = 8
DEFAULT_TIER = 0
DEFAULT_COOLDOWN_HOURS = 48


class CompanyListUnavailable(RuntimeError):
    """No company list is configured. Nothing to sync, so nothing is built."""


@dataclass(frozen=True, slots=True)
class ImportSourcesReport:
    fetched: int
    resolved: tuple[CompanyRef, ...] = ()
    unresolved: tuple[CompanyRef, ...] = ()
    blocked: tuple[tuple[CompanyRef, str], ...] = ()
    deactivated: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {
            "fetched": self.fetched,
            "resolved": len(self.resolved),
            "unresolved": len(self.unresolved),
            "blocked": len(self.blocked),
            "deactivated": len(self.deactivated),
        }

    @property
    def summary(self) -> str:
        return (
            f"fetched {self.fetched}: {len(self.resolved)} resolved, "
            f"{len(self.unresolved)} unresolved, {len(self.blocked)} blocked, "
            f"{len(self.deactivated)} deactivated"
        )


@dataclass(slots=True)
class ImportSourcesRun:
    provider: CompanyListProvider
    store: CompanyListStore
    list_source: str
    now: datetime
    fetcher: Fetcher | None = None
    refresh: bool = False
    tier: int = DEFAULT_TIER
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS
    # Resolution is up to one HTTP probe per vendor per slug candidate, run one after
    # another, so a caller that prints nothing until the end looks hung on a watchlist of
    # any size.
    on_company: Callable[[CompanyRef, ResolvedCompany | None], None] | None = None
    # (completed, total) after each company, for run-progress reporting.
    on_progress: Callable[[int, int], None] | None = None

    _done: int = field(default=0, init=False, repr=False)
    _total: int = field(default=0, init=False, repr=False)

    def _announce(self, ref: CompanyRef, outcome: ResolvedCompany | None) -> None:
        """`None` means the row was already resolved and was not re-probed."""
        if self.on_company is not None:
            self.on_company(ref, outcome)
        self._done += 1
        if self.on_progress is not None:
            self.on_progress(self._done, self._total)

    def _is_fresh(self, existing: ImportCompany | None) -> bool:
        return (
            existing is not None
            and existing.last_probed_at is not None
            and self.now - existing.last_probed_at < RECHECK_AFTER
        )

    def _resolve(
        self, ref: CompanyRef, existing: ImportCompany | None, fetch: Fetcher
    ) -> ResolvedCompany:
        """Runs on a worker thread: network only, no store access. A stale resolved row
        first re-checks the board it already has, which refreshes its open-role count
        with one request; only when that board is gone does it resolve from scratch."""
        if existing is not None and existing.pinned and existing.board_type and existing.slug:
            # The user chose this board: refresh its role count, never replace it. If it
            # has gone, say so and keep the choice, so it recovers when the board returns.
            probe = probe_board(fetch, existing.board_type, existing.slug)
            if probe.exists:
                return ResolvedCompany(
                    ref,
                    ImportStatus.RESOLVED,
                    existing.board_type,
                    existing.slug,
                    open_roles=probe.open_roles,
                )
            where = f"{existing.board_type}/{existing.slug}"
            return ResolvedCompany(
                ref,
                ImportStatus.BLOCKED,
                existing.board_type,
                existing.slug,
                last_error=f"{where} (set by hand): {probe.blocked_reason or 'board not found'}",
            )
        if (
            not self.refresh
            and existing is not None
            and existing.status is ImportStatus.RESOLVED
            and existing.board_type
            and existing.slug
        ):
            probe = probe_board(fetch, existing.board_type, existing.slug)
            if probe.exists:
                return ResolvedCompany(
                    ref,
                    ImportStatus.RESOLVED,
                    existing.board_type,
                    existing.slug,
                    open_roles=probe.open_roles,
                )
        return resolve_company(ref, fetch)

    def execute(self) -> ImportSourcesReport:
        fetch = self.fetcher or http_fetcher()
        refs = list(self.provider.fetch())
        self._done, self._total = 0, len(refs)
        # (position in the list, ref, outcome-or-None); sorted at the end so the report
        # keeps the list's order however the probes happen to finish.
        settled: list[tuple[int, CompanyRef, ResolvedCompany | None]] = []
        pending: list[tuple[int, CompanyRef, ImportCompany | None]] = []

        for index, ref in enumerate(refs):
            self.store.upsert_import_company(self.list_source, ref, self.now)
            existing = self.store.get_import_company(self.list_source, ref.external_id)
            skip = (
                not self.refresh
                and existing is not None
                and self._is_fresh(existing)
                and existing.status in (ImportStatus.RESOLVED, ImportStatus.UNRESOLVED)
            )
            if skip:
                assert existing is not None
                settled.append((index, ref, None))
                self._announce(ref, None)
                continue
            pending.append((index, ref, existing))

        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as pool:
            futures = {
                pool.submit(self._resolve, ref, existing, fetch): (index, ref)
                for index, ref, existing in pending
            }
            for future in as_completed(futures):
                index, ref = futures[future]
                outcome = future.result()
                self._announce(ref, outcome)
                self.store.record_resolution(
                    self.list_source,
                    ref.external_id,
                    outcome.status,
                    outcome.board_type,
                    outcome.slug,
                    outcome.last_error,
                    self.now,
                    open_roles=outcome.open_roles,
                )
                if outcome.status is ImportStatus.RESOLVED:
                    assert outcome.board_type is not None and outcome.slug is not None
                    self.store.insert_imported_target(
                        SourceTarget(
                            outcome.board_type, outcome.slug, self.tier, self.cooldown_hours
                        ),
                        self.now,
                        ref.external_id,
                    )
                settled.append((index, ref, outcome))

        resolved: list[CompanyRef] = []
        unresolved: list[CompanyRef] = []
        blocked: list[tuple[CompanyRef, str]] = []
        for _, ref, result in sorted(settled, key=lambda item: item[0]):
            if result is None:
                # Skipped as fresh: its standing is whatever the ledger already says.
                row = self.store.get_import_company(self.list_source, ref.external_id)
                assert row is not None
                status = row.status
            else:
                status = result.status
            if status is ImportStatus.RESOLVED:
                resolved.append(ref)
            elif status is ImportStatus.BLOCKED:
                reason = (result.last_error if result else None) or "blocked"
                blocked.append((ref, reason))
            else:
                unresolved.append(ref)

        deactivated = self.store.deactivate_missing_import_companies(
            self.list_source, (ref.external_id for ref in refs), self.now
        )
        return ImportSourcesReport(
            fetched=len(refs),
            resolved=tuple(resolved),
            unresolved=tuple(unresolved),
            blocked=tuple(blocked),
            deactivated=deactivated,
        )


def build_run(
    provider: CompanyListProvider | None,
    store: CompanyListStore,
    list_source: str,
    now: datetime,
    **options: object,
) -> ImportSourcesRun:
    """Refuses before anything is built when no list is configured, the same way
    `discover_sources.build_run` refuses without a search provider."""
    if provider is None:
        raise CompanyListUnavailable(
            "no company list is configured — set company_lists.notion.database_id or "
            "company_lists.csv.path in config.yaml"
        )
    return ImportSourcesRun(
        provider=provider,
        store=store,
        list_source=list_source,
        now=now,
        **options,  # type: ignore[arg-type]
    )
