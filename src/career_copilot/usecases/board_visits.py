"""Every board the crawler visits, in the order the next run will reach them."""

from __future__ import annotations

from collections.abc import Sequence

from career_copilot.domain.models import SourceTarget
from career_copilot.domain.sources import BoardVisit
from career_copilot.ports import StateStore
from career_copilot.usecases.discover import visit_key


def board_visits(store: StateStore, config_targets: Sequence[SourceTarget]) -> list[BoardVisit]:
    """The store's boards plus the targets written in config.yaml, which the store does
    not hold, longest-waiting first (the order `DiscoverRun` visits them in)."""
    rows = list(store.list_board_visits())
    known = {(row.source, row.slug) for row in rows}
    visited = store.last_scraped_by_target()
    for target in config_targets:
        if (target.source, target.identifier) in known:
            continue
        rows.append(
            BoardVisit(
                name=target.identifier,
                source=target.source,
                slug=target.identifier,
                origin="config",
                status="resolved",
                tier=target.tier,
                added_at=None,
                open_roles=None,
                last_visited_at=visited.get((target.source, target.identifier)),
            )
        )
    rows.sort(key=lambda row: visit_key(row.tier, row.last_visited_at))
    return rows
