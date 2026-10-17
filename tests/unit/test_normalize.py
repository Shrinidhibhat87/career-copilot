from __future__ import annotations

import pytest

from career_copilot.domain.normalize import (
    canonical_url,
    content_hash,
    location_segments,
    normalize_company,
    normalize_location,
    normalize_title,
    to_posting,
)
from tests.conftest import make_raw


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Delivery Hero SE", "delivery hero"),
        ("Delivery Hero", "delivery hero"),
        ("Acme GmbH", "acme"),
        ("Zalando  SE ", "zalando"),
        ("N26 GmbH & Co. KG", "n26"),
        ("Foo, Inc.", "foo"),
    ],
)
def test_company_legal_suffixes_are_dropped(raw: str, expected: str) -> None:
    assert normalize_company(raw) == expected


def test_company_aliases_resolve() -> None:
    assert normalize_company("DH", {"dh": "delivery hero"}) == "delivery hero"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Senior Engineer (m/w/d)", "senior engineer"),
        ("Senior Engineer (f/m/x)", "senior engineer"),
        ("Senior Engineer m/w/d", "senior engineer"),
        ("Senior Engineer (all genders)", "senior engineer"),
        ("Senior  Engineer", "senior engineer"),
    ],
)
def test_gender_tags_do_not_split_identity(raw: str, expected: str) -> None:
    assert normalize_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Berlin, Germany", "berlin"),
        ("Berlin", "berlin"),
        ("München", "munich"),
        ("Remote", "remote"),
        ("Remote - Europe", "remote"),
        ("Frankfurt am Main, Germany", "frankfurt"),
    ],
)
def test_location_reduces_to_city_token(raw: str, expected: str) -> None:
    assert normalize_location(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The shape 156 of the stored Berlin postings arrive in. One city, written three
        # times, used to collapse into "berlin berlin" and match no city list at all.
        ("Berlin, Berlin, Germany", ("berlin",)),
        ("Berlin, Berlin, Germany; Berlin, Berlin, Germany", ("berlin",)),
        ("Munich - Berlin", ("munich", "berlin")),
        ("Berlin; London; Munich", ("berlin", "london", "munich")),
        ("Berlin, Berlin, Germany; Frankfurt a. M.", ("berlin", "frankfurt")),
        ("Aurora, IL; Irving, TX; Berlin", ("aurora", "il", "irving", "tx", "berlin")),
        ("Remote - Europe", ("remote",)),
        ("Remote US, West Coast", ("remote us", "west coast")),
        ("Remote, New York, Canada, Dubai", ("remote", "new york", "canada", "dubai")),
        ("Remote Canada", ("remote canada",)),
        ("Remote in Germany", ("remote",)),
        ("Home Office", ("remote",)),
        ("Berlin, Germany", ("berlin",)),
        ("Germany", ()),
        ("", ()),
    ],
)
def test_a_location_is_every_place_it_names(raw: str, expected: tuple[str, ...]) -> None:
    assert location_segments(raw) == expected


def test_the_primary_segment_is_what_identity_still_uses() -> None:
    """`location_norm` feeds `fingerprint`, so it stays one token per posting."""
    assert normalize_location("Berlin; London; Munich") == "berlin"
    assert normalize_location("Remote US, West Coast") == "remote"


def test_canonical_url_strips_tracking_but_keeps_identity_params() -> None:
    url = "https://Boards.Greenhouse.io/acme/jobs/1?gh_jid=99&utm_source=li&ref=x#apply"
    assert canonical_url(url) == "https://boards.greenhouse.io/acme/jobs/1?gh_jid=99"


def test_canonical_url_ignores_trailing_slash() -> None:
    assert canonical_url("https://x.com/a/") == canonical_url("https://x.com/a")


def test_content_hash_ignores_whitespace_only_changes() -> None:
    assert content_hash("Build   AI\nsystems") == content_hash("build ai systems")


def test_content_hash_changes_with_real_edits() -> None:
    assert content_hash("Build AI systems") != content_hash("Build AI systems in Rust")


def test_fingerprint_matches_same_job_across_sources() -> None:
    a = to_posting(make_raw(source="greenhouse", company="Acme GmbH", location="Berlin, Germany"))
    b = to_posting(
        make_raw(
            source="lever",
            source_job_id="xyz",
            company="Acme",
            title="Forward Deployed Engineer (f/m/x)",
            location="Berlin",
            url="https://jobs.lever.co/acme/xyz",
        )
    )
    assert a.fingerprint == b.fingerprint


def test_fingerprint_separates_different_roles() -> None:
    a = to_posting(make_raw(title="Forward Deployed Engineer"))
    b = to_posting(make_raw(title="Data Engineer"))
    assert a.fingerprint != b.fingerprint


def test_remote_is_inferred_from_location_when_unset() -> None:
    assert to_posting(make_raw(location="Remote, Germany")).remote is True
    assert to_posting(make_raw(location="Berlin, Germany")).remote is False
