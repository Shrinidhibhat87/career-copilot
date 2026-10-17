"""Export a filter-behaviour corpus from a local jobs database.

The corpus pins `domain/filters.py` against real public ATS postings rather than
hand-written strings, so a refactor of the rules is proved harmless on the wording
boards actually publish. Postings are public job adverts; nothing personal is stored.

Sampling keeps every posting the filters did not reject, plus a capped sample of
rejections covering each distinct reason combination, so the corpus stays small
without losing a rule.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

# Enough rejections per distinct reason combination to catch an ordering change,
# without carrying 990 near-identical adverts in the repository.
PER_REASON_CAP = 2

# The unit separator the store joins reasons with.
_SEP = "\x1f"

FIELDS = (
    "source",
    "source_job_id",
    "company",
    "title",
    "location",
    "canonical_url",
    "description",
    "employment_type",
    "remote",
)


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(conn.execute("SELECT * FROM jobs ORDER BY id"))
    finally:
        conn.close()


def _split(value: str) -> list[str]:
    return [item for item in value.split(_SEP) if item]


def reason_kinds(reasons: str, warnings: str) -> str:
    """The rules a posting tripped, without the values they name."""
    parts = [value.split(":")[0] for value in (reasons + _SEP + warnings).split(_SEP) if value]
    return "|".join(sorted(set(parts)))


def sample(rows: list[sqlite3.Row], per_reason_cap: int = PER_REASON_CAP) -> list[sqlite3.Row]:
    """Everything that survived the filters, plus a capped slice of each rejection."""
    kept: list[sqlite3.Row] = []
    seen: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["filter_outcome"] != "reject":
            kept.append(row)
            continue
        # Keyed by reason *kind*, not the rendered reason: "location outside Germany:
        # Paris" and "…: Madrid" exercise one rule, and keying on the whole string
        # would carry a copy of the corpus per city.
        key = reason_kinds(row["filter_reasons"], row["filter_warnings"])
        if seen[key] < per_reason_cap:
            seen[key] += 1
            kept.append(row)
    return kept


def to_entry(row: sqlite3.Row) -> dict[str, Any]:
    entry: dict[str, Any] = {field: row[field] for field in FIELDS}
    entry["remote"] = bool(row["remote"]) if row["remote"] is not None else None
    # The outcome the *previous* implementation recorded for this posting, under the
    # config.yaml in force at discovery time. Committed with the posting so a rewrite of
    # the rules is checked against what actually shipped, not only against itself.
    entry["filter_outcome"] = row["filter_outcome"]
    entry["filter_reasons"] = list(_split(row["filter_reasons"]))
    entry["filter_warnings"] = list(_split(row["filter_warnings"]))
    return entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/jobs.db", type=Path)
    parser.add_argument("--out", default="tests/fixtures/ats_postings.json", type=Path)
    parser.add_argument("--per-reason-cap", default=PER_REASON_CAP, type=int)
    args = parser.parse_args()

    rows = sample(_rows(args.db), args.per_reason_cap)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps([to_entry(row) for row in rows], indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(rows)} postings to {args.out}")


if __name__ == "__main__":
    main()
