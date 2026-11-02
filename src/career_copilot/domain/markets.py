"""Market packs (spec 018): which country a person searches in, as a bundle held in code.

Pure, no I/O. A pack names the searched sources it turns on; only `ready` packs can be
enabled. Adding a country is one more entry in `MARKETS`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

DEFAULT = "DE"


@dataclass(frozen=True, slots=True)
class Market:
    code: str
    name: str
    language: str  # key into the language patterns
    country_words: tuple[str, ...]
    cities: tuple[str, ...]  # lowercase; how a saved city names its country
    sources: tuple[str, ...]  # searched sources this pack turns on
    radius_km: int
    ready: bool


@dataclass(frozen=True, slots=True)
class MarketSetting:
    version: int
    codes: tuple[str, ...]
    confirmed_at: datetime


def _soon(code: str, name: str, language: str, *words: str) -> Market:
    return Market(code, name, language, words, (), (), 25, False)


MARKETS: dict[str, Market] = {
    "DE": Market(
        code="DE",
        name="Germany",
        language="german",
        country_words=("germany", "deutschland"),
        cities=(
            "berlin",
            "munich",
            "münchen",
            "muenchen",
            "hamburg",
            "cologne",
            "köln",
            "koeln",
            "frankfurt",
            "stuttgart",
            "düsseldorf",
            "duesseldorf",
            "leipzig",
            "dresden",
            "hannover",
            "nuremberg",
            "nürnberg",
            "bremen",
            "dortmund",
            "essen",
        ),
        sources=("jobsuche",),
        radius_km=25,
        ready=True,
    ),
    # Listed so the person can see what is planned; no source is claimed until one is verified.
    **{
        m.code: m
        for m in (
            _soon("AT", "Austria", "german", "austria", "österreich"),
            _soon("BE", "Belgium", "dutch", "belgium", "belgië", "belgique"),
            _soon("CH", "Switzerland", "german", "switzerland", "schweiz", "suisse"),
            _soon("ES", "Spain", "spanish", "spain", "españa"),
            _soon("FR", "France", "french", "france"),
            _soon("GB", "United Kingdom", "english", "united kingdom", "uk", "england", "scotland"),
            _soon("IE", "Ireland", "english", "ireland"),
            _soon("IN", "India", "english", "india"),
            _soon("IT", "Italy", "italian", "italy", "italia"),
            _soon("NL", "Netherlands", "dutch", "netherlands", "the netherlands", "nederland"),
            _soon("PL", "Poland", "polish", "poland", "polska"),
            _soon("PT", "Portugal", "portuguese", "portugal"),
            _soon("SE", "Sweden", "swedish", "sweden", "sverige"),
            _soon("US", "United States", "english", "united states", "usa", "u.s."),
            _soon("ZA", "South Africa", "english", "south africa"),
        )
    },
}


def enabled(codes: Sequence[str]) -> tuple[Market, ...]:
    """The ready packs among `codes`, in pack order. Nothing chosen means Germany."""
    chosen = tuple(m for code, m in MARKETS.items() if code in codes and m.ready)
    return chosen or (MARKETS[DEFAULT],)


def propose(cities: Sequence[str], cv_text: str | None) -> tuple[str, ...]:
    """Ready markets the saved cities point to, else the ones the CV names, else none.

    Cities first: a CV shows where someone worked, not where they want to work."""
    places = {c.strip().lower() for c in cities}
    from_cities = tuple(c for c, m in MARKETS.items() if m.ready and places & set(m.cities))
    if from_cities or not cv_text:
        return from_cities
    text = cv_text.lower()
    return tuple(
        c
        for c, m in MARKETS.items()
        if m.ready and any(re.search(rf"\b{re.escape(w)}\b", text) for w in m.country_words)
    )
