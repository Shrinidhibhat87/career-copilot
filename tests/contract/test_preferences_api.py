"""The HTTP contract for /api/preferences, including every documented status."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from career_copilot.adapters.api import app as api
from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, ModelsSpec, RoleSpec
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.resume_detect import DetectedLanguage, ResumeDetections
from career_copilot.ports import LLMCallError
from tests.conftest import make_raw


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client over a scratch database, so no test touches the real `data/jobs.db`."""
    config = Config.load()
    # Independent of whatever `config.yaml` wires up locally (e.g. a local backend for
    # `triage`) — the `scoring.*` fallback path is what the tests below assert against.
    config.models = ModelsSpec()
    config.db_path = str(tmp_path / "jobs.db")
    monkeypatch.setattr(api, "_config", config)
    with TestClient(api.app) as test_client:
        yield test_client


DETECTIONS = ResumeDetections(
    roles=("Data Engineering",),
    skills=("Kafka",),
    languages=(
        DetectedLanguage("German", "B2"),
        DetectedLanguage("Kannada", None),
    ),
    skill_terms=("kafka",),
)


@pytest.fixture
def stored_cv(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stored CV whose detections are fixed, so the tests assert the prefill and not the
    detector."""
    monkeypatch.setattr(api, "detect", lambda text, terms: DETECTIONS)
    with SqliteStore(api._config.db_path) as store:
        store.save_cv("cv.pdf", "Data engineer. Kafka.", datetime(2026, 9, 5, tzinfo=UTC))


def submit(body: dict[str, Any]) -> dict[str, Any]:
    """A full form body, as the screen sends it."""
    form: dict[str, Any] = {
        "roleFamilies": [{"term": "ai/ml engineering", "label": "AI/ML Engineering"}],
        "coreSkills": [{"term": "python"}, {"term": "agentic"}],
        "excludedStack": [],
        "excludedRoleTerms": [],
        "seniority": ["mid", "senior"],
        "allowedCities": [{"term": "berlin"}, {"term": "remote"}, {"term": ""}],
        "preferredCities": [{"term": "berlin"}],
        "flaggedCities": [{"term": "munich"}],
        "allowRemote": True,
        "requirePermanent": True,
        "languages": [{"name": "German", "level": "B1"}],
        "maxPerCompany": 2,
        "batchSize": 6,
        "notes": "Munich only with a Berlin office.",
    }
    form.update(body)
    return form


# --- GET ------------------------------------------------------------------


def test_an_unsaved_database_returns_the_bootstrap(client: TestClient) -> None:
    body = client.get("/api/preferences").json()
    assert body["version"] == 0
    assert body["seniority"] == ["mid", "senior"]
    assert {"term": "python", "label": "Python", "fromResume": False} in body["coreSkills"]


def test_terms_travel_with_their_display_label(client: TestClient) -> None:
    skills = client.get("/api/preferences").json()["coreSkills"]
    assert {"term": "llm", "label": "LLM / RAG", "fromResume": False} in skills


def test_resume_detections_prefill_and_are_badged(client: TestClient, stored_cv: None) -> None:
    body = client.get("/api/preferences").json()
    skills = {entry["term"]: entry for entry in body["coreSkills"]}
    assert skills["kafka"]["fromResume"] is True
    roles = {entry["term"]: entry for entry in body["roleFamilies"]}
    assert roles["data engineering"]["fromResume"] is True
    german = next(item for item in body["languages"] if item["name"] == "German")
    # Unsaved seed defaults yield to explicitly detected resume levels.
    assert german["level"] == "B2"
    assert german["fromResume"] is True


def test_a_detected_language_without_a_level_is_not_invented(
    client: TestClient, stored_cv: None
) -> None:
    names = [item["name"] for item in client.get("/api/preferences").json()["languages"]]
    assert "Kannada" not in names


def test_a_saved_version_is_not_overridden_by_detections(
    client: TestClient, stored_cv: None
) -> None:
    client.put("/api/preferences", json=submit({"roleFamilies": []}))
    body = client.get("/api/preferences").json()
    assert body["version"] == 1
    assert body["roleFamilies"] == []


# --- PUT ------------------------------------------------------------------


def test_a_save_returns_the_new_version_and_survives_a_restart(client: TestClient) -> None:
    body = client.put("/api/preferences", json=submit({})).json()
    assert body["preferences"]["version"] == 1
    assert body["preferences"]["notes"] == "Munich only with a Berlin office."
    assert client.get("/api/preferences").json()["version"] == 1


def test_a_hand_typed_skill_is_normalized_into_a_term(client: TestClient) -> None:
    body = client.put(
        "/api/preferences", json=submit({"coreSkills": [{"term": "  Apache  Kafka "}]})
    ).json()
    assert body["preferences"]["coreSkills"][0]["term"] == "apache kafka"


def test_a_validation_failure_is_422_with_one_message_per_field(client: TestClient) -> None:
    response = client.put("/api/preferences", json=submit({"allowedCities": [], "batchSize": 0}))
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert set(errors) == {"allowed_cities", "batch_size"}


def test_a_rejected_save_writes_nothing(client: TestClient) -> None:
    client.put("/api/preferences", json=submit({"allowedCities": []}))
    assert client.get("/api/preferences").json()["version"] == 0


def test_an_unreadable_value_is_422_not_500(client: TestClient) -> None:
    response = client.put("/api/preferences", json=submit({"batchSize": "lots"}))
    assert response.status_code == 422


def test_a_save_refilters_stored_jobs_and_reports_what_moved(client: TestClient) -> None:
    store = SqliteStore(api._config.db_path)
    posting = to_posting(make_raw(location="Berlin, Germany"))
    store.upsert_job(posting, apply_hard_filters(posting), datetime.now(UTC))
    store.close()

    body = client.put("/api/preferences", json=submit({"allowedCities": [{"term": "hamburg"}]}))
    report = body.json()["refiltered"]
    assert report["examined"] == 1
    assert report["transitions"] == {"pass→reject": 1}
    assert body.json()["reopenedForScoring"] == 0


def test_an_operational_change_reopens_nothing(client: TestClient) -> None:
    store = SqliteStore(api._config.db_path)
    posting = to_posting(make_raw())
    store.upsert_job(posting, apply_hard_filters(posting), datetime.now(UTC))
    store.close()

    first = client.put("/api/preferences", json=submit({})).json()
    again = client.put("/api/preferences", json=submit({"batchSize": 25, "maxPerCompany": 5}))
    # The batch size never reaches a prompt, so no job's result could differ because of it.
    assert first["reopenedForScoring"] == again.json()["reopenedForScoring"]


# --- POST /preview --------------------------------------------------------


def stored_titles(titles: list[str]) -> None:
    with SqliteStore(api._config.db_path) as store:
        for number, title in enumerate(titles):
            posting = to_posting(
                make_raw(source_job_id=str(number), title=title, url=f"https://x.test/{number}")
            )
            store.upsert_job(posting, apply_hard_filters(posting), datetime.now(UTC))


def test_a_preview_reports_the_impact_of_an_excluded_term_without_saving(
    client: TestClient,
) -> None:
    stored_titles(["Senior Machine Learning Manager", "Data Engineer Manager", "Data Engineer"])

    response = client.post(
        "/api/preferences/preview", json=submit({"excludedRoleTerms": [{"term": "manager"}]})
    )

    body = response.json()
    assert response.status_code == 200
    assert body["excludedRoleTerms"] == {"manager": {"matches": 2, "newlyHidden": 2}}
    assert body["transitions"] == {"pass→reject": 2}
    assert body["hidden"] == 2
    # Nothing was saved, and no stored job moved.
    assert client.get("/api/preferences").json()["version"] == 0
    with SqliteStore(api._config.db_path) as store:
        assert {job.filter_outcome for job in store.all_active_jobs()} == {"pass"}


def test_a_save_after_a_preview_does_what_the_preview_said(client: TestClient) -> None:
    stored_titles(["Senior Machine Learning Manager", "Data Engineer"])
    draft = submit({"excludedRoleTerms": [{"term": "manager"}]})

    previewed = client.post("/api/preferences/preview", json=draft).json()
    saved = client.put("/api/preferences", json=draft).json()["refiltered"]

    assert previewed["transitions"] == saved["transitions"]
    assert previewed["changed"] == saved["changed"]


def test_a_preview_of_an_invalid_draft_is_a_422_that_writes_nothing(client: TestClient) -> None:
    response = client.post("/api/preferences/preview", json=submit({"allowedCities": []}))

    assert response.status_code == 422
    assert "allowed_cities" in response.json()["detail"]["errors"]
    assert client.get("/api/preferences").json()["version"] == 0


# --- POST /extract --------------------------------------------------------


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def fake_client(monkeypatch: pytest.MonkeyPatch, client: FakeLLM) -> None:
    monkeypatch.setattr(api, "_extraction_client", lambda: client)


def test_extraction_returns_changes_and_their_cost(
    client: TestClient, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client(
        monkeypatch,
        FakeLLM(
            default={
                "changes": [
                    {
                        "field": "flagged_cities",
                        "action": "add",
                        "value": "hamburg",
                        "rationale": "the note names Hamburg",
                    }
                ]
            },
            cost_per_call_eur=0.02,
        ),
    )
    body = client.post("/api/preferences/extract", json={"notes": "Hamburg is fine."}).json()
    assert body["changes"][0]["field"] == "flagged_cities"
    assert body["changes"][0]["label"] == "Hamburg"
    assert body["costEur"] == 0.02


def test_a_hostile_proposal_never_reaches_the_screen(
    client: TestClient, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client(
        monkeypatch,
        FakeLLM(
            default={
                "changes": [
                    {"field": "db_path", "action": "set", "value": "/etc/passwd", "rationale": ""}
                ]
            }
        ),
    )
    body = client.post("/api/preferences/extract", json={"notes": "anything"}).json()
    assert body["changes"] == []


def test_the_server_reads_the_env_file_like_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API is its own process: a key in .env is invisible unless it is loaded here.

    Without this the extraction route answers 409 forever, however the key is configured,
    which is exactly the failure this pins.
    """
    import importlib

    calls: list[int] = []
    monkeypatch.setattr("dotenv.load_dotenv", lambda *args, **kwargs: calls.append(1))
    importlib.reload(api)
    assert calls, "adapters/api/app.py must load .env at import, as cli.py does"


def test_no_api_key_is_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    response = client.post("/api/preferences/extract", json={"notes": "Hamburg is fine."})
    assert response.status_code == 409


def test_extraction_client_uses_the_configured_local_role(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    roles: list[object] = []
    api._config.models.roles["triage"] = RoleSpec(backend="ollama", model="qwen3:4b")

    def record(role: object, recorder: object = None) -> FakeLLM:
        roles.append(role)
        return FakeLLM()

    monkeypatch.setattr(api, "build_redactor", lambda config: None)
    monkeypatch.setattr(api, "build_client", record)
    api._extraction_client()
    assert roles == [api._config.role("triage")]


def test_a_provider_failure_is_502(
    client: TestClient, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing:
        def complete(self, *args: object, **kwargs: object) -> None:
            raise LLMCallError("provider down")

    monkeypatch.setattr(api, "_extraction_client", Failing)
    response = client.post("/api/preferences/extract", json={"notes": "Hamburg is fine."})
    assert response.status_code == 502


def test_extraction_writes_nothing(
    client: TestClient, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client(
        monkeypatch,
        FakeLLM(
            default={
                "changes": [
                    {
                        "field": "flagged_cities",
                        "action": "add",
                        "value": "hamburg",
                        "rationale": "",
                    }
                ]
            }
        ),
    )
    client.post("/api/preferences/extract", json={"notes": "Hamburg is fine."})
    assert client.get("/api/preferences").json()["version"] == 0


@pytest.mark.parametrize("patch", [{"allowRemote": "false"}, {"batchSize": 1.5}])
def test_save_rejects_values_that_would_be_silently_coerced(
    client: TestClient, patch: dict[str, Any]
) -> None:
    response = client.put("/api/preferences", json=submit(patch))
    assert response.status_code == 422
    assert client.get("/api/preferences").json()["version"] == 0


def test_failed_refilter_rolls_back_preferences_and_job_updates(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteStore(api._config.db_path)
    posting = to_posting(make_raw())
    job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), datetime.now(UTC))
    store.close()
    original = SqliteStore.update_filter_result

    def fail_after_update(self: SqliteStore, *args: Any, **kwargs: Any) -> None:
        original(self, *args, **kwargs)
        raise RuntimeError("simulated failure after updating a job")

    monkeypatch.setattr(SqliteStore, "update_filter_result", fail_after_update)
    with pytest.raises(RuntimeError, match="simulated failure"):
        client.put("/api/preferences", json=submit({"allowedCities": ["hamburg"]}))
    with SqliteStore(api._config.db_path) as check:
        assert check.load_current() is None
        jobs = check.all_active_jobs()
        assert next(job for job in jobs if job.id == job_id).filter_outcome.value == "pass"


def test_extraction_uses_unsaved_form_without_persisting_it(
    client: TestClient, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = FakeLLM(default={"changes": []})
    fake_client(monkeypatch, model)
    response = client.post(
        "/api/preferences/extract",
        json={"notes": "Keep my skills", "preferences": submit({"coreSkills": ["elixir"]})},
    )
    assert response.status_code == 200
    assert "Core skills: elixir" in model.calls[0]
    assert client.get("/api/preferences").json()["version"] == 0


def test_proposals_return_canonical_terms_for_browser_removal(
    client: TestClient, with_key: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client(
        monkeypatch,
        FakeLLM(
            default={
                "changes": [
                    {
                        "field": "core_skills",
                        "action": "remove",
                        "value": "  Python  ",
                        "rationale": "",
                    }
                ]
            }
        ),
    )
    response = client.post("/api/preferences/extract", json={"notes": "Remove Python"})
    assert response.json()["changes"][0]["value"] == "python"
