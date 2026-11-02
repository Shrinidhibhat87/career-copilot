"""The markets an account searches in, as the packs in force (spec 018)."""

from __future__ import annotations

from career_copilot.domain.markets import Market, enabled
from career_copilot.ports import StateStore


def enabled_markets(store: StateStore) -> tuple[Market, ...]:
    setting = store.market_setting()
    return enabled(setting.codes if setting else ())
