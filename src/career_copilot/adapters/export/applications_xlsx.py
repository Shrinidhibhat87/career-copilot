"""The Excel file of every application and where it stands (spec 021)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

from career_copilot.domain.pipeline import LABELS, STAGES

HEADERS = (
    "Role",
    "Company",
    "Location",
    "Source",
    "Score",
    "Applied on",
    "Stage",
    "Last change",
    "Days quiet",
    "Note",
    "Job link",
)
# A cell starting with one of these is a formula to Excel; job titles are third-party text.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _day(value: object) -> datetime | str:
    try:
        # Excel has no timezones; the stored times are UTC.
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return _text(value)


def build(cards: Sequence[dict[str, object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Applications"
    sheet.append(list(HEADERS))
    for card in cards:
        sheet.append(
            [
                _text(card["title"]),
                _text(card["company"]),
                _text(card["location"]),
                _text(card["source"]),
                card["score"],
                _day(card["appliedOn"]),
                LABELS[str(card["stage"])],
                _day(card["lastChange"]),
                card["daysQuiet"],
                _text(card["note"]),
                _text(card["url"]),
            ]
        )
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith(FORMULA_STARTS):
                cell.data_type = "s"
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"
    for column, width in zip(
        "ABCDEFGHIJK", (38, 24, 24, 14, 8, 12, 14, 12, 11, 40, 50), strict=True
    ):
        sheet.column_dimensions[column].width = width

    summary = book.create_sheet("Summary")
    summary.append(["Stage", "Applications"])
    for stage in STAGES:
        summary.append([LABELS[stage], sum(1 for c in cards if c["stage"] == stage)])
    summary.append(["Total", len(cards)])
    book.active = 0

    out = BytesIO()
    book.save(out)
    return out.getvalue()
