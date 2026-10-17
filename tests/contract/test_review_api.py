"""HTTP contract for the review screen: the list, the card, and the append-only writes."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import review
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import ScoreStage
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from career_copilot.domain.sources import CompanyRef
from tests.conftest import make_raw

NOW = datetime(2026, 9, 6, tzinfo=UTC)


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, Config]]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()
    app.include_router(review.router(lambda: config))
    with SqliteStore(config.db_path):
        pass
    yield TestClient(app), config


def seed(config: Config, **overrides: object) -> int:
    with SqliteStore(config.db_path) as store:
        posting = to_posting(make_raw(**overrides))
        job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
        return job_id


def score(config: Config, job_id: int, value: int, **overrides: object) -> None:
    with SqliteStore(config.db_path) as store:
        content_hash = store._conn.execute(
            "SELECT content_hash FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()[0]
        defaults: dict[str, object] = {
            "job_id": job_id,
            "stage": ScoreStage.MATCH,
            "score": value,
            "verdict": ScoreVerdict.from_score(value),
            "rubric_version": RUBRIC_VERSION,
            "model": "test-model",
            "prompt_hash": "p",
            "content_hash": content_hash,
        }
        defaults.update(overrides)
        store.record_score(Score(**defaults), NOW, cost_eur=0.02)  # type: ignore[arg-type]


def test_the_list_cuts_at_the_threshold_and_says_what_it_hid(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    strong = seed(config, source_job_id="1", url="https://x.test/1", title="Data Engineer A")
    weak = seed(config, source_job_id="2", url="https://x.test/2", title="Data Engineer B")
    score(config, strong, 80)
    score(config, weak, 40)

    page = client.get("/api/review?threshold=60").json()
    assert [item["id"] for item in page["items"]] == [strong]
    assert (page["kept"], page["dropped"]) == (1, 1)

    below = client.get("/api/review?threshold=60&includeBelow=true").json()
    assert [item["id"] for item in below["items"]] == [strong, weak]
    assert [item["belowThreshold"] for item in below["items"]] == [False, True]


def test_the_list_dates_each_posting_and_states_the_retirement_horizon(
    api: tuple[TestClient, Config],
) -> None:
    """The screen warns that a posting may be gone before the user clicks a dead link, so
    it needs both the last sighting and the horizon the crawler retires one at."""
    client, config = api
    job = seed(config, source_job_id="1", url="https://x.test/1", title="Data Engineer A")
    score(config, job, 80)

    page = client.get("/api/review").json()
    assert page["inactiveAfterDays"] == config.inactive_after_days
    assert page["items"][0]["lastSeenAt"]


def test_the_list_is_bounded_and_counts_what_it_hides(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    for n in range(55):
        job_id = seed(
            config, source_job_id=str(n), url=f"https://x.test/{n}", title=f"Data Engineer {n}"
        )
        score(config, job_id, 60)
    seed(config, source_job_id="u", url="https://x.test/u", title="Data Engineer U")

    page = client.get("/api/review").json()
    assert (len(page["items"]), page["kept"], page["unscored"]) == (10, 55, 1)
    assert len(client.get("/api/review?limit=50").json()["items"]) == 50
    assert client.get("/api/review?limit=51").status_code == 422
    assert len(client.get("/api/review?limit=50&includeBelow=true").json()["items"]) == 50


def test_an_unscored_posting_says_so_rather_than_scoring_zero(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job_id = seed(config, title="Data Engineer")
    assert client.get("/api/review").json()["items"] == []
    [item] = client.get("/api/review?includeBelow=true").json()["items"]
    assert (item["id"], item["score"], item["scoreStage"]) == (job_id, None, None)


def test_the_card_carries_the_claims_the_rules_and_the_cost(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job_id = seed(config, title="Data Engineer", location="Berlin; London")
    score(
        config,
        job_id,
        75,
        reasons=("Ships Python pipelines",),
        evidence_refs=("Built SQLDumpReader in Python",),
        gaps=("No Databricks",),
    )
    card = client.get(f"/api/jobs/{job_id}").json()
    assert card["reasons"] == ["Ships Python pipelines"]
    assert card["evidence"] == ["Built SQLDumpReader in Python"]
    assert card["gaps"] == ["No Databricks"]
    assert card["verdict"] == str(ScoreVerdict.STRONG)
    assert any("london" in warning for warning in card["filterWarnings"])
    assert card["costEur"] == 0.02


def test_an_unknown_job_is_a_404(api: tuple[TestClient, Config]) -> None:
    client, _ = api
    assert client.get("/api/jobs/404").status_code == 404


def test_a_label_is_stored_and_read_back_on_the_card(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job_id = seed(config, title="Data Engineer")

    response = client.post(
        "/api/reviews", json={"jobId": job_id, "label": "good_fit", "note": "worth a call"}
    )
    assert response.status_code == 201
    assert response.json()["rerankedNothing"] is True

    card = client.get(f"/api/jobs/{job_id}").json()
    assert (card["label"], card["note"]) == ("good_fit", "worth a call")


def test_changing_a_label_appends_rather_than_overwrites(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job_id = seed(config, title="Data Engineer")
    client.post("/api/reviews", json={"jobId": job_id, "label": "maybe"})
    client.post("/api/reviews", json={"jobId": job_id, "label": "not_relevant"})

    with SqliteStore(config.db_path) as store:
        rows = store._conn.execute("SELECT label FROM reviews ORDER BY id").fetchall()
    assert [row["label"] for row in rows] == ["maybe", "not_relevant"]
    assert client.get(f"/api/jobs/{job_id}").json()["label"] == "not_relevant"


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"jobId": 1, "label": "brilliant"}, "label"),
        ({"label": "good_fit"}, "jobId"),
    ],
)
def test_a_label_outside_the_vocabulary_is_refused(
    api: tuple[TestClient, Config], payload: dict[str, object], field: str
) -> None:
    client, config = api
    seed(config, title="Data Engineer")
    response = client.post("/api/reviews", json=payload)
    assert response.status_code == 422
    assert field in response.json()["detail"]["errors"]


def test_a_label_for_an_unknown_job_is_a_404(api: tuple[TestClient, Config]) -> None:
    client, _ = api
    assert client.post("/api/reviews", json={"jobId": 404, "label": "good_fit"}).status_code == 404


def test_a_correction_names_the_rule_it_disputes(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job_id = seed(config, title="Data Engineer")
    response = client.post(
        "/api/corrections",
        json={
            "jobId": job_id,
            "kind": "rule",
            "target": "no profile-stack signal",
            "note": "it names Python",
        },
    )
    assert response.status_code == 201
    with SqliteStore(config.db_path) as store:
        row = store._conn.execute("SELECT job_id, kind, target FROM corrections").fetchone()
    assert (row["job_id"], row["kind"], row["target"]) == (
        job_id,
        "rule",
        "no profile-stack signal",
    )


def test_company_url_is_the_imported_homepage(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, company="Acme GmbH")
    score(config, job, 80)
    with SqliteStore(config.db_path) as store:
        store.upsert_import_company(
            "csv", CompanyRef("1", "ACME GmbH", "https://acme.example"), NOW
        )

    assert client.get("/api/review").json()["items"][0]["companyUrl"] == "https://acme.example"
    assert client.get(f"/api/jobs/{job}").json()["companyUrl"] == "https://acme.example"


def test_company_url_falls_back_to_the_careers_page_a_person_can_read(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    job = seed(config)  # posted at boards.greenhouse.io/acme/jobs/1001
    score(config, job, 80)

    item = client.get("/api/review").json()["items"][0]
    assert item["companyUrl"] == "https://boards.greenhouse.io/acme"


def test_company_url_is_null_with_neither(api: tuple[TestClient, Config]) -> None:
    client, config = api
    job = seed(config, url="https://x.test/1")
    score(config, job, 80)

    assert client.get("/api/review").json()["items"][0]["companyUrl"] is None


def test_a_remote_role_in_another_country_is_a_flagged_location_mismatch_scored_below_ten(
    api: tuple[TestClient, Config],
) -> None:
    client, config = api
    berlin = seed(config, source_job_id="1", url="https://x.test/1", title="Data Engineer A")
    toronto = seed(
        config,
        source_job_id="2",
        url="https://x.test/2",
        title="Data Engineer B",
        location="Toronto",
        remote=True,
    )
    score(config, berlin, 80)
    score(config, toronto, 30)

    default = client.get("/api/review?threshold=30").json()
    assert [item["id"] for item in default["items"]] == [berlin]
    assert default["items"][0]["locationMismatch"] is False

    everything = client.get("/api/review?threshold=30&includeBelow=true").json()
    by_id = {item["id"]: item for item in everything["items"]}
    assert by_id[toronto]["locationMismatch"] is True
    assert by_id[toronto]["score"] == 9
    assert by_id[toronto]["belowThreshold"] is True
    assert client.get(f"/api/jobs/{toronto}").json()["locationMismatch"] is True
