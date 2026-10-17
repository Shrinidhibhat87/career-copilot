"""HTTP for adding one company's job board by typing its name.

Three steps, each its own request so the user confirms before anything is crawled:
`lookup` guesses the board from the name, `search` asks the discovery agent (only when
the user chose to, because it spends the discovery budget), and `confirm` adds a board
after checking it again. A driving adapter: `usecases/find_board.py` does the work.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from career_copilot.adapters.search.tavily import KEY_ENV
from career_copilot.adapters.sources.ats import Fetcher
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.sources import SourceProposal
from career_copilot.usecases.board_visits import board_visits
from career_copilot.usecases.find_board import AddBoardError, add_board, find_board

# Takes the config and a company name; returns the boards the agent proposed.
WebSearch = Callable[[Config, str], list[SourceProposal]]


def _now() -> datetime:
    return datetime.now(UTC)


def _name(body: dict[str, Any]) -> str:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 200:
        raise HTTPException(422, detail={"errors": {"name": "expected a company name"}})
    return name


def router(
    get_config: Callable[[], Config],
    fetch: Fetcher | None = None,
    web_search: WebSearch | None = None,
) -> APIRouter:
    routes = APIRouter()

    @routes.post("/api/sources/lookup")
    def lookup(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        """Guess the board from the name. Changes nothing."""
        result = find_board(_name(body), fetch)
        hit = result.hit
        return {
            "name": result.name,
            "found": hit is not None,
            "boardType": hit.board_type if hit else None,
            "slug": hit.slug if hit else None,
            "openRoles": hit.open_roles if hit else None,
            "tried": list(result.tried),
            # A blocked probe means try again; searching the web would not help.
            "blockedReason": result.blocked_reason,
            "canSearchWeb": hit is None
            and result.blocked_reason is None
            and bool(os.environ.get(KEY_ENV)),
        }

    @routes.post("/api/sources/lookup/search")
    def search(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        """Ask the discovery agent for one company's board. Spends discovery budget, so the
        page only calls it after the user said yes. Nothing is stored."""
        name = _name(body)
        if not os.environ.get(KEY_ENV):
            raise HTTPException(
                409,
                detail={"errors": {"search": f"web search needs {KEY_ENV} in .env"}},
            )
        if web_search is None:
            raise HTTPException(
                409, detail={"errors": {"search": "web search is not available here"}}
            )
        proposals = web_search(get_config(), name)
        return {
            "name": name,
            "boards": [
                {
                    "boardType": p.board_type,
                    "slug": p.slug,
                    "company": p.company,
                    "openRoles": p.job_count,
                    "sampleTitles": list(p.sample_titles),
                    "evidence": p.evidence,
                }
                for p in proposals
            ],
        }

    @routes.post("/api/sources/lookup/confirm", status_code=201)
    def confirm(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        """Add a board to the crawl, after checking it against the real vendor again."""
        board_type = str(body.get("boardType") or "")
        slug = str(body.get("slug") or "")
        with SqliteStore(get_config().db_path) as store:
            try:
                added = add_board(
                    store,
                    board_type,
                    slug,
                    _now(),
                    fetch,
                    str(body.get("name") or "")[:200],
                    {(t.source, t.identifier) for t in get_config().source_targets()},
                )
            except AddBoardError as exc:
                raise HTTPException(422, detail={"errors": {"board": str(exc)}}) from None
        return {
            "boardType": added.board_type,
            "slug": added.slug,
            "openRoles": None if added.already_listed else added.open_roles,
            "alreadyListed": added.already_listed,
            "filledListEntry": added.filled_list_entry,
        }

    @routes.get("/api/sources/boards")
    def boards() -> list[dict[str, object]]:
        """Every board the crawler visits, next-to-visit first, with when each was last
        visited. Read only."""
        config = get_config()
        with SqliteStore(config.db_path) as store:
            visits = board_visits(store, config.source_targets())
        return [
            {
                "name": visit.name,
                "boardType": visit.source,
                "slug": visit.slug,
                "origin": visit.origin,
                "status": visit.status,
                "addedAt": visit.added_at.isoformat() if visit.added_at else None,
                "openRoles": visit.open_roles,
                "lastVisitedAt": visit.last_visited_at.isoformat()
                if visit.last_visited_at
                else None,
            }
            for visit in visits
        ]

    return routes


def agent_search(config: Config, name: str) -> list[SourceProposal]:
    """The discovery agent aimed at one company. Nothing is stored: the proposals come
    back to the page, and the user's confirm is what adds a board.

    Heavy imports are local: loading the model stack is only worth it once someone asks.
    """
    from datetime import UTC, datetime

    from career_copilot.adapters.llm.backends import probe
    from career_copilot.adapters.pii.factory import load_overrides
    from career_copilot.adapters.search.tavily import TavilySearch
    from career_copilot.agents.discovery.agent import DiscoveryLimits, build_model
    from career_copilot.domain.budget import RunLimits
    from career_copilot.domain.owner_identity import OwnerIdentity
    from career_copilot.usecases.discover_sources import build_run
    from career_copilot.usecases.pipeline import current_preferences

    role = config.role("discovery")
    status = probe(role)
    if not status.usable or status.tools is False:
        detail = status.detail or "tool calling is unavailable"
        raise HTTPException(
            409,
            detail={"errors": {"search": f"discovery model unavailable: {role.model} ({detail})"}},
        )
    spec = config.discovery
    with SqliteStore(config.db_path) as store:
        run = build_run(
            build_model(role),
            store,
            TavilySearch(),
            current_preferences(store, config),
            load_overrides(config.pii.identity_path) or OwnerIdentity(),
            datetime.now(UTC),
            company=name,
            target_proposals=1,
            max_searches=spec.max_searches,
            max_eur=spec.max_eur_per_run,
            limits=DiscoveryLimits(spec.max_requests, spec.max_tool_calls),
            run_limits=RunLimits(max_seconds=spec.max_seconds),
            persist=False,
        )
        return list(run.execute().proposals)
