"""The applications workbook (spec 021)."""

from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from career_copilot.adapters.export.applications_xlsx import HEADERS, build


def card(title: str, stage: str, **extra: object) -> dict[str, object]:
    return {
        "title": title,
        "company": "Acme",
        "location": "Berlin",
        "source": "greenhouse",
        "score": 82,
        "appliedOn": "2026-10-01T09:00:00+00:00",
        "stage": stage,
        "lastChange": "2026-10-20T09:00:00+00:00",
        "daysQuiet": 22,
        "note": "",
        "url": "https://x.test/1",
        "hidden": False,
    } | extra


def test_one_row_per_application_and_a_summary_per_stage() -> None:
    book = load_workbook(BytesIO(build([card("A", "applied"), card("B", "offer", hidden=True)])))
    assert book.sheetnames == ["Applications", "Summary"]
    rows = list(book["Applications"].values)
    assert rows[0] == HEADERS
    assert [(r[0], r[6]) for r in rows[1:]] == [("A", "Applied"), ("B", "Offer")]
    assert {r[0]: r[1] for r in list(book["Summary"].values)[1:]} == {
        "Applied": 1,
        "Interviewing": 0,
        "Offer": 1,
        "Rejected": 0,
        "Withdrawn": 0,
        "Total": 2,
    }


def test_text_that_looks_like_a_formula_stays_text() -> None:
    book = load_workbook(BytesIO(build([card('=HYPERLINK("http://evil")', "applied")])))
    cell = book["Applications"]["A2"]
    assert cell.data_type == "s"
    assert cell.value == '=HYPERLINK("http://evil")'


def test_an_empty_list_still_gives_a_valid_file() -> None:
    book = load_workbook(BytesIO(build([])))
    assert list(book["Applications"].values) == [HEADERS]
    assert list(book["Summary"].values)[-1] == ("Total", 0)
