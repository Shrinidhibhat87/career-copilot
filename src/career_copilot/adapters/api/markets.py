"""HTTP for the markets an account searches in (spec 018).

A driving adapter only. What a pack holds and how a market is proposed live in
`domain/markets.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.markets import MARKETS, enabled, propose
from career_copilot.usecases.apply_preferences import apply_preferences
from career_copilot.usecases.pipeline import current_preferences


def router(get_config: Callable[[], Config]) -> APIRouter:
    routes = APIRouter(prefix="/api/markets", tags=["markets"])

    def view(config: Config) -> dict[str, Any]:
        with SqliteStore(config.db_path) as store:
            saved = store.market_setting()
            prefs = store.load_current()
            cv = store.current_cv()
        cities = prefs.allowed_cities if prefs else config.filters.allowed_cities
        return {
            "packs": [
                {
                    "code": m.code,
                    "name": m.name,
                    "language": m.language,
                    "ready": m.ready,
                    "sources": list(m.sources),
                }
                for m in MARKETS.values()
            ],
            "enabled": [m.code for m in enabled(saved.codes if saved else ())],
            "confirmed": saved is not None,
            "proposal": list(propose(list(cities), cv.text if cv else None)),
        }

    @routes.get("")
    def get() -> dict[str, Any]:
        return view(get_config())

    @routes.put("")
    def put(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        codes = body.get("codes")
        if not isinstance(codes, list) or not codes or not all(isinstance(c, str) for c in codes):
            raise HTTPException(400, detail={"errors": {"codes": "choose at least one market"}})
        bad = [c for c in codes if c not in MARKETS or not MARKETS[c].ready]
        if bad:
            raise HTTPException(
                400, detail={"errors": {"codes": f"not available yet: {', '.join(bad)}"}}
            )
        config = get_config()
        with SqliteStore(config.db_path) as store:
            store.save_market_setting(tuple(dict.fromkeys(codes)), datetime.now(UTC))
            # Stored jobs carry the outcome computed under the old markets, as with a
            # preferences save.
            apply_preferences(
                store,
                current_preferences(store, config),
                model=config.scoring.match_model,
                rubric_version=config.scoring.rubric_version,
            )
        return view(config)

    return routes
