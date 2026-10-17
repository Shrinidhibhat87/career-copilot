"""`CompanyListProvider` contract, run against every implementation: the CSV adapter and
an in-memory fake, so the port's promises hold independent of any one adapter.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from career_copilot.adapters.sources.company_lists import CsvCompanyList
from career_copilot.domain.sources import CompanyRef
from career_copilot.ports import CompanyListProvider


class FakeCompanyList:
    def __init__(self, refs: Sequence[CompanyRef]) -> None:
        self.refs = refs

    def fetch(self) -> Sequence[CompanyRef]:
        return self.refs


def _csv(tmp_path: Path) -> CompanyListProvider:
    path = tmp_path / "companies.csv"
    path.write_text("name,url\nAcme GmbH,https://boards.greenhouse.io/acme\nPeec AI,\n")
    return CsvCompanyList(path)


def _fake(tmp_path: Path) -> CompanyListProvider:
    return FakeCompanyList(
        [
            CompanyRef("1", "Acme GmbH", "https://boards.greenhouse.io/acme"),
            CompanyRef("2", "Peec AI"),
        ]
    )


@pytest.fixture(params=[_csv, _fake], ids=["csv", "fake"])
def provider(request: pytest.FixtureRequest, tmp_path: Path) -> CompanyListProvider:
    make: Callable[[Path], CompanyListProvider] = request.param
    return make(tmp_path)


def test_fetch_returns_company_refs_with_names(provider: CompanyListProvider) -> None:
    refs = provider.fetch()
    assert [ref.name for ref in refs] == ["Acme GmbH", "Peec AI"]
    assert all(isinstance(ref, CompanyRef) for ref in refs)


def test_external_ids_are_unique_and_non_empty(provider: CompanyListProvider) -> None:
    ids = [ref.external_id for ref in provider.fetch()]
    assert all(ids)
    assert len(set(ids)) == len(ids)


def test_a_re_fetch_yields_the_same_external_ids(provider: CompanyListProvider) -> None:
    first = [ref.external_id for ref in provider.fetch()]
    second = [ref.external_id for ref in provider.fetch()]
    assert first == second


def test_a_missing_url_is_none_not_empty(provider: CompanyListProvider) -> None:
    assert provider.fetch()[1].url is None
