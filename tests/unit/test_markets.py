"""Market packs (spec 018): which ready packs are in force and what is proposed."""

from __future__ import annotations

from career_copilot.domain.markets import DEFAULT, MARKETS, enabled, propose


def test_nothing_chosen_means_the_default_market() -> None:
    assert [m.code for m in enabled(())] == [DEFAULT]


def test_a_pack_that_is_not_ready_cannot_be_in_force() -> None:
    assert [m.code for m in enabled(("ZA", "IN", "US", "GB"))] == [DEFAULT]
    assert [m.code for m in enabled(("ZA", "DE"))] == ["DE"]


def test_the_big_planned_markets_are_listed() -> None:
    assert {"FR", "NL", "PL", "ES", "GB", "US", "IT", "IN", "ZA"} <= set(MARKETS)
    assert [c for c, m in MARKETS.items() if m.ready] == ["DE"]


def test_planned_packs_claim_no_source() -> None:
    assert all(m.sources == () for m in MARKETS.values() if not m.ready)


def test_saved_cities_are_proposed_first() -> None:
    assert propose(["Berlin", "remote", ""], None) == ("DE",)
    # The CV names another country, but the saved cities decide.
    assert propose(["Munich"], "worked in India for ten years") == ("DE",)


def test_the_cv_is_the_fallback() -> None:
    assert propose(["remote"], "Software engineer, Hamburg, Germany") == ("DE",)
    assert propose([], "lives in Deutschland") == ("DE",)


def test_nothing_is_proposed_without_evidence() -> None:
    assert propose(["remote", ""], None) == ()
    assert propose([], "Engineer in Paris") == ()
    # A pack that is not ready is never proposed.
    assert propose([], "Engineer in India") == ()
