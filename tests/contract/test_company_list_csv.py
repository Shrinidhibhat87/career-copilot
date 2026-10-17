"""The CSV adapter: the free-standing test harness for the resolution pipeline, proving
the CompanyListProvider port boundary before Notion's auth/network complexity is added.
"""

from __future__ import annotations

from pathlib import Path

from career_copilot.adapters.sources.company_lists import CsvCompanyList


def write_csv(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "companies.csv"
    path.write_text(body)
    return path


def test_a_name_only_row_has_no_url(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "name,url\nAcme GmbH,\n")
    refs = CsvCompanyList(path).fetch()
    assert len(refs) == 1
    assert refs[0].name == "Acme GmbH"
    assert refs[0].url is None


def test_a_row_with_a_url_keeps_it(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "name,url\nAcme GmbH,https://boards.greenhouse.io/acme\n")
    refs = CsvCompanyList(path).fetch()
    assert refs[0].url == "https://boards.greenhouse.io/acme"


def test_wrapped_and_blank_rows_are_normalized(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "name,url\n{CERN},\n,\n  Extra   Space  Inc ,\n")
    refs = CsvCompanyList(path).fetch()
    names = [ref.name for ref in refs]
    assert names == ["CERN", "Extra Space Inc"]


def test_external_id_is_stable_across_re_reads(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "name,url\nAcme GmbH,\n")
    first = CsvCompanyList(path).fetch()
    second = CsvCompanyList(path).fetch()
    assert first[0].external_id == second[0].external_id


def test_a_missing_url_column_is_tolerated(tmp_path: Path) -> None:
    path = write_csv(tmp_path, "name\nAcme GmbH\n")
    refs = CsvCompanyList(path).fetch()
    assert refs[0].url is None
