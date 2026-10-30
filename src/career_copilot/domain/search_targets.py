"""The searches a person's preferences imply for a job board that is searched, not browsed.

An ATS board is one company; a national board is asked "this role, near here". Pure:
preferences in, targets out, so the same preferences always give the same searches and
nothing needs configuring by hand. No place means no search: a board for one country
cannot answer "anywhere", and guessing a place would fetch jobs the user did not ask for.
"""

from __future__ import annotations

from urllib.parse import urlencode

from career_copilot.domain.models import SourceTarget
from career_copilot.domain.preferences import Preferences

# Each search costs one request per job found, so the product is kept small.
MAX_ROLES = 2
MAX_PLACES = 2
# After the hand-written company boards, so a bounded run reaches them first.
SEARCH_TIER = 1


def search_targets(
    source: str, preferences: Preferences, *, radius_km: int = 25, cooldown_hours: int = 24
) -> list[SourceTarget]:
    roles = list(preferences.title_terms or preferences.role_families)[:MAX_ROLES]
    # `remote` and blanks sit in the same list as cities, and are not places to search.
    places = [c for c in preferences.allowed_cities if c.strip() and c.lower() != "remote"][
        :MAX_PLACES
    ]
    if not roles or not places:
        return []
    return [
        SourceTarget(
            source=source,
            identifier=urlencode({"was": role, "wo": place, "umkreis": radius_km}),
            tier=SEARCH_TIER,
            cooldown_hours=cooldown_hours,
        )
        for role in roles
        for place in places
    ]
