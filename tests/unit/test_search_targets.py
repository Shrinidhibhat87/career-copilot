"""Searches implied by preferences, the ping, and the doctor line. No network."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from _pytest.capture import CaptureFixture

import career_copilot.__main__ as cli
from career_copilot.adapters.sources.jobsuche import JobsucheSource
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, JobsucheSpec
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.search_targets import search_targets
from career_copilot.usecases.pipeline import crawl_targets

PREFS = Preferences(
    title_terms=("data engineer", "ml engineer", "ignored third"),
    allowed_cities=("Berlin", "Köln", "ignored third"),
)


def test_each_role_is_searched_in_each_place_up_to_a_small_cap() -> None:
    targets = search_targets("jobsuche", PREFS, radius_km=30)
    assert [t.identifier for t in targets] == [
        "was=data+engineer&wo=Berlin&umkreis=30",
        "was=data+engineer&wo=K%C3%B6ln&umkreis=30",
        "was=ml+engineer&wo=Berlin&umkreis=30",
        "was=ml+engineer&wo=K%C3%B6ln&umkreis=30",
    ]
    assert {t.source for t in targets} == {"jobsuche"} and {t.tier for t in targets} == {1}


def test_no_place_or_no_role_means_no_search() -> None:
    assert search_targets("jobsuche", Preferences(title_terms=("data engineer",))) == []
    assert search_targets("jobsuche", Preferences(allowed_cities=("Berlin",))) == []


def test_remote_and_blank_are_not_places() -> None:
    prefs = Preferences(title_terms=("data engineer",), allowed_cities=("remote", "", "Berlin"))
    assert [t.identifier for t in search_targets("jobsuche", prefs)] == [
        "was=data+engineer&wo=Berlin&umkreis=25"
    ]


def test_role_families_stand_in_when_there_are_no_title_terms() -> None:
    prefs = Preferences(role_families=("Data Engineer",), allowed_cities=("Berlin",))
    assert len(search_targets("jobsuche", prefs)) == 1


def test_crawl_targets_adds_the_searches_after_the_seed_and_can_be_switched_off() -> None:
    with SqliteStore(":memory:") as store:
        on = crawl_targets(Config(), store, PREFS)
        assert [t.source for t in on] == ["jobsuche"] * 4
        off = Config(jobsuche=JobsucheSpec(enabled=False))
        assert crawl_targets(off, store, PREFS) == []
        assert crawl_targets(Config(), store) == []


def test_ping_asks_for_one_result_and_reports_the_total() -> None:
    asked: list[str] = []

    def fetch(url: str) -> Any:
        asked.append(url)
        return {"maxErgebnisse": 1234, "ergebnisliste": []}

    assert JobsucheSource(fetcher=fetch).ping() == 1234
    assert len(asked) == 1 and "size=1" in asked[0] and "/pc/v6/jobs?" in asked[0]


def _doctor_line(monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str], fetch: Any) -> str:
    class Fake(JobsucheSource):
        def __init__(self) -> None:
            super().__init__(fetcher=fetch)

    monkeypatch.setattr(cli, "SEARCH_SOURCES", {"jobsuche": Fake})
    cli._report_sources(Config())
    return capsys.readouterr().out


def test_doctor_reports_a_reachable_source(
    monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    out = _doctor_line(monkeypatch, capsys, lambda url: {"maxErgebnisse": 42})
    assert out.startswith("ok   source jobsuche") and "42 postings" in out


def test_doctor_warns_and_does_not_raise_when_a_source_is_down(
    monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    def down(url: str) -> Any:
        raise httpx.ConnectError("no route")

    out = _doctor_line(monkeypatch, capsys, down)
    assert out.startswith("warn source jobsuche") and "no route" in out


def test_doctor_sends_nothing_when_the_source_is_switched_off(
    monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "SEARCH_SOURCES", {"jobsuche": lambda: 1 / 0})
    cli._report_sources(Config(jobsuche=JobsucheSpec(enabled=False)))
    assert capsys.readouterr().out == ""
