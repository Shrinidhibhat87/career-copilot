"""Point a company the resolver could not find at its real board, by hand.

The resolver guesses slugs from a company's name, so it misses a company whose board is
called something else. This takes the answer from the user (a careers-page link, or
`vendor/slug`), checks it against the real board the same way a sync does, and only then
records it. A wrong answer changes nothing.
"""

from __future__ import annotations

from datetime import datetime

from career_copilot.adapters.sources.ats import SOURCE_TYPES, Fetcher, http_fetcher
from career_copilot.domain.company_resolution import extract_ats_hint, probe_board
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.sources import ImportCompany
from career_copilot.ports import CompanyListStore
from career_copilot.usecases.import_sources import DEFAULT_COOLDOWN_HOURS, DEFAULT_TIER


class SetBoardError(ValueError):
    """The answer could not be used. The message is written for the person who typed it."""


def parse_board(text: str) -> tuple[str, str] | None:
    """`ashby/n26` or any careers URL a known vendor hosts. `None` when it is neither."""
    text = text.strip()
    head, _, tail = text.partition("/")
    slug = tail.strip("/").split("/")[0]
    if head.lower() in SOURCE_TYPES and slug:
        return head.lower(), slug
    return extract_ats_hint(text)


def set_board(
    store: CompanyListStore,
    company: ImportCompany,
    text: str,
    now: datetime,
    fetch: Fetcher | None = None,
) -> tuple[str, str, int | None]:
    """Returns `(board_type, slug, open_roles)` once the board is verified and recorded."""
    parsed = parse_board(text)
    if parsed is None:
        raise SetBoardError(
            "That is not a board link I recognise. Paste the company's careers-page link "
            f"or write vendor/slug, for example ashby/n26. Vendors: {', '.join(SOURCE_TYPES)}."
        )
    board_type, slug = parsed
    probe = probe_board(fetch or http_fetcher(), board_type, slug)
    if not probe.exists:
        if probe.blocked_reason:
            raise SetBoardError(
                f"Could not check {board_type}/{slug} right now ({probe.blocked_reason}). "
                "Nothing was changed; try again later."
            )
        raise SetBoardError(f"{board_type} has no board called '{slug}'. Nothing was changed.")
    store.record_manual_resolution(
        company.list_source, company.external_id, board_type, slug, probe.open_roles, now
    )
    store.insert_imported_target(
        SourceTarget(board_type, slug, DEFAULT_TIER, DEFAULT_COOLDOWN_HOURS),
        now,
        company.external_id,
    )
    return board_type, slug, probe.open_roles
