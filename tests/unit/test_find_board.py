"""Type one company name, see the board it is on, then add it."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.usecases.find_board import (
    AddBoardError,
    add_board,
    find_board,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
BOARD = json.loads((FIXTURES / "greenhouse_board.json").read_text())


def _missing() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test")
    return httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, request=request)
    )


def fetcher(*, has: str | None) -> Any:
    """Serves the Greenhouse fixture only for the board whose slug is `has`."""

    def fetch(url: str) -> Any:
        if has is not None and "greenhouse" in url and f"/{has}/" in url + "/":
            return BOARD
        raise _missing()

    return fetch


def test_a_name_that_matches_a_board_is_found() -> None:
    result = find_board("Acme GmbH", fetcher(has="acme"))
    assert result.hit is not None
    assert (result.hit.board_type, result.hit.slug) == ("greenhouse", "acme")
    assert result.hit.open_roles


def test_an_unknown_name_says_what_was_tried() -> None:
    result = find_board("Zzyzx Industries", fetcher(has=None))
    assert result.hit is None
    assert result.blocked_reason is None
    assert "zzyzx-industries" in result.tried


def test_a_blocked_probe_is_not_reported_as_a_miss() -> None:
    def blocked(url: str) -> Any:
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            "429", request=request, response=httpx.Response(429, request=request)
        )

    result = find_board("Acme", blocked)
    assert result.hit is None
    assert result.blocked_reason is not None


def test_a_blank_name_is_refused() -> None:
    with pytest.raises(ValueError):
        find_board("   ", fetcher(has=None))


def test_confirming_adds_one_manual_board_and_rechecks_it(now: datetime) -> None:
    with SqliteStore(":memory:") as store:
        added = add_board(store, "greenhouse", "acme", now, fetcher(has="acme"))
        assert added.open_roles
        assert [(t.source, t.identifier) for t in store.list_source_targets()] == [
            ("greenhouse", "acme")
        ]
        assert store.imported_target_origins()[("greenhouse", "acme")] == "manual"
        # Adding it again is harmless.
        add_board(store, "greenhouse", "acme", now, fetcher(has="acme"))
        assert len(store.list_source_targets()) == 1


def test_a_wrong_board_is_refused_and_changes_nothing(now: datetime) -> None:
    with SqliteStore(":memory:") as store, pytest.raises(AddBoardError):
        add_board(store, "greenhouse", "nope", now, fetcher(has=None))
    with SqliteStore(":memory:") as store:
        assert list(store.list_source_targets()) == []


def test_an_unknown_vendor_is_refused(now: datetime) -> None:
    with SqliteStore(":memory:") as store, pytest.raises(AddBoardError):
        add_board(store, "myspace", "acme", now, fetcher(has="acme"))
