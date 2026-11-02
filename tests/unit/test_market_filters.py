"""Hard filters follow the enabled markets (spec 018). Germany alone behaves as it always did."""

from __future__ import annotations

from dataclasses import replace

from career_copilot.domain.filters import FilterConfig, apply_hard_filters
from career_copilot.domain.markets import MARKETS
from career_copilot.domain.models import FilterOutcome
from career_copilot.domain.normalize import location_segments, to_posting
from tests.conftest import make_raw

DE = MARKETS["DE"]
FR = replace(MARKETS["FR"], ready=True)  # a ready pack, as a later source would make it
CITIES = frozenset({"berlin", "paris", "remote", ""})


def check(location: str, *markets: object, **raw: object):  # type: ignore[no-untyped-def]
    cfg = FilterConfig(allowed_cities=CITIES, markets=markets or (DE,))  # type: ignore[arg-type]
    return apply_hard_filters(to_posting(make_raw(location=location, **raw)), cfg)


def test_germany_alone_rejects_other_countries_with_the_old_wording() -> None:
    result = check("Madrid, Spain")
    assert result.outcome is FilterOutcome.REJECT
    assert result.reasons[0] == "location outside Germany: Madrid, Spain"


def test_an_enabled_market_makes_its_country_part_of_the_place() -> None:
    # Some boards write the country without a separator: "Paris France" is one place.
    assert check("Paris France").outcome is FilterOutcome.REJECT
    assert check("Paris France", DE, FR).outcome is FilterOutcome.PASS
    assert check("Berlin Germany", DE, FR).outcome is FilterOutcome.PASS


def test_a_country_that_is_not_enabled_stays_attached_to_the_city() -> None:
    result = check("Berlin Germany", FR)
    assert result.outcome is FilterOutcome.REJECT
    assert result.reasons[0].startswith("location outside France")


def test_several_markets_name_themselves_as_your_markets() -> None:
    result = check("Madrid, Spain", DE, FR)
    assert result.reasons[0] == "location outside your markets: Madrid, Spain"


def test_remote_stays_open_in_every_market() -> None:
    assert check("Remote", FR).outcome is FilterOutcome.PASS
    result = check("Remote US", DE, FR)
    assert "remote role with location outside your markets: Remote US" in result.warnings


def test_the_german_language_rule_applies_only_with_a_german_market() -> None:
    ad = {
        "description": (
            "Permanent full-time role building agentic AI systems in Python. "
            "Fluent German is required."
        )
    }
    assert check("Berlin, Germany", **ad).outcome is not FilterOutcome.PASS
    assert check("Paris, France", FR, **ad).outcome is FilterOutcome.PASS
    assert check("Paris, France", DE, FR, **ad).outcome is not FilterOutcome.PASS


def test_relocation_wording_names_the_persons_own_places() -> None:
    ad = {"description": "Permanent full-time role. Relocation to Paris is required."}
    assert check("Lyon, France", **ad).reasons[0].startswith("location outside Germany")
    assert check("Lyon, France", DE, FR, **ad).reasons[0].startswith("location outside")
    far = {"description": "Permanent full-time role. Relocation to Lyon is required."}
    assert check("Madrid, Spain", **far).reasons[0].startswith("relocation required")


def test_segments_strip_only_the_given_countries() -> None:
    assert location_segments("Paris France") == ("paris france",)
    assert location_segments("Paris France", ("france", "fr")) == ("paris",)
    assert location_segments("Berlin Germany") == ("berlin",)
