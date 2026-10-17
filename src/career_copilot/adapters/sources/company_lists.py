"""CompanyListProvider adapters. CSV is the free-standing test harness for the
resolution pipeline before Notion's auth/network complexity is added.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Sequence
from pathlib import Path

from career_copilot.domain.sources import CompanyRef, normalize_company_name


class CsvCompanyList:
    """A two-column `name,url` CSV. `url` is optional. A CSV row has no natural id of
    its own, so `external_id` is a stable hash of the normalized name."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def fetch(self) -> Sequence[CompanyRef]:
        refs: list[CompanyRef] = []
        with self._path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                raw_name = (row.get("name") or "").strip()
                if not raw_name:
                    continue
                name = normalize_company_name(raw_name)
                url = (row.get("url") or "").strip() or None
                external_id = hashlib.sha1(name.lower().encode("utf-8")).hexdigest()
                refs.append(CompanyRef(external_id=external_id, name=name, url=url))
        return refs
