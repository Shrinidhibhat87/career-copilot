"""HTTP contract for condensing the intent log: draft a note, then approve it in one go."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from career_copilot.adapters.api import intent
from career_copilot.adapters.pii.redactor import Redaction
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.condenser import build_condenser
from career_copilot.config import Config
from career_copilot.domain.intent import CONDENSE_AT_WORDS, MAX_CONDENSED_CHARS
from career_copilot.usecases.condense_intent import Redact

NOTE = "I want forward-deployed roles in Berlin, never adtech."


class Model:
    """Records every prompt it is sent and answers with `reply`, or fails."""

    def __init__(self, reply: str = NOTE, fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.reply = reply
        self.fail = fail

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.prompts.append(str(messages))
        if self.fail:
            raise RuntimeError("backend down")
        return ModelResponse(parts=[TextPart(self.reply)])

    def factory(self, _config: Config) -> Agent[None, str]:
        return build_condenser(FunctionModel(self.respond))


def no_redaction(_config: Config) -> Redact | None:
    return None


def fake_redaction(_config: Config) -> Redact | None:
    """Stands in for Presidio: one name, one shared placeholder across every text."""

    def redact(texts: Sequence[str]) -> Redaction:
        cleaned = [text.replace("Marlena", "<|PERSON_1|>") for text in texts]
        return Redaction(cleaned, {"PERSON": 1}, {"<|PERSON_1|>": "Marlena"})

    return redact


def make_client(
    tmp_path: Path,
    model: Model,
    redact_factory: Callable[[Config], Redact | None] = no_redaction,
) -> TestClient:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    app = FastAPI()
    app.include_router(intent.router(lambda: config, model.factory, redact_factory))
    return TestClient(app)


@pytest.fixture
def model() -> Model:
    return Model()


@pytest.fixture
def client(tmp_path: Path, model: Model) -> Iterator[TestClient]:
    yield make_client(tmp_path, model)


def seed(client: TestClient, *texts: str) -> list[int]:
    return [client.post("/api/intent", json={"text": text}).json()["id"] for text in texts]


def test_condense_drafts_a_note_and_changes_nothing(client: TestClient, model: Model) -> None:
    seed(client, "FDE roles in Berlin", "Not adtech, ever")
    response = client.post("/api/intent/condense")
    assert response.status_code == 200
    assert response.json() == {"draft": NOTE}
    assert len(model.prompts) == 1
    assert model.prompts[0].index("FDE roles in Berlin") < model.prompts[0].index("Not adtech")
    assert len(client.get("/api/intent").json()["entries"]) == 2


def test_condense_with_no_entries_is_422_and_calls_no_model(
    client: TestClient, model: Model
) -> None:
    response = client.post("/api/intent/condense")
    assert response.status_code == 422
    assert "text" in response.json()["detail"]["errors"]
    assert model.prompts == []


def test_a_failing_model_is_502_with_a_reason(tmp_path: Path) -> None:
    client = make_client(tmp_path, Model(fail=True))
    seed(client, "FDE roles in Berlin")
    response = client.post("/api/intent/condense")
    assert response.status_code == 502
    assert "backend down" in response.json()["detail"]["errors"]["model"]


def test_an_empty_answer_is_502(tmp_path: Path) -> None:
    client = make_client(tmp_path, Model(reply="   "))
    seed(client, "FDE roles in Berlin")
    response = client.post("/api/intent/condense")
    assert response.status_code == 502
    assert "model" in response.json()["detail"]["errors"]


def test_placeholders_never_reach_the_model_and_are_restored(tmp_path: Path) -> None:
    model = Model(reply="I am <|PERSON_1|> and want FDE roles in Berlin.")
    client = make_client(tmp_path, model, fake_redaction)
    seed(client, "Ask Marlena about FDE roles", "Marlena wants Berlin")
    response = client.post("/api/intent/condense")
    assert response.status_code == 200
    assert response.json()["draft"] == "I am Marlena and want FDE roles in Berlin."
    assert "Marlena" not in model.prompts[0]
    assert "1. Ask <|PERSON_1|> about FDE roles" in model.prompts[0]
    assert "2. <|PERSON_1|> wants Berlin" in model.prompts[0]


def test_approve_archives_every_entry_and_keeps_one_condensed_note(
    client: TestClient, tmp_path: Path
) -> None:
    old = seed(client, "FDE roles in Berlin", "Not adtech, ever")
    response = client.post("/api/intent/condense/approve", json={"text": f"  {NOTE}  "})
    assert response.status_code == 201
    body = response.json()
    assert body["condenseAt"] == CONDENSE_AT_WORDS
    assert body["activeWords"] == len(NOTE.split())
    [entry] = body["entries"]
    assert entry["text"] == NOTE and entry["kind"] == "condensed"
    assert entry["id"] not in old
    assert client.get("/api/intent").json() == body
    # Archived, not deleted: the old words are still in the table.
    with SqliteStore(str(tmp_path / "jobs.db")) as store:
        rows = store._conn.execute(
            "SELECT id FROM intent_entries WHERE archived_at IS NOT NULL ORDER BY id"
        ).fetchall()
    assert [row["id"] for row in rows] == old


@pytest.mark.parametrize("text", ["", "   ", None, 7, "x" * (MAX_CONDENSED_CHARS + 1)])
def test_approve_refuses_empty_or_too_long_and_keeps_the_log(
    client: TestClient, text: object
) -> None:
    seed(client, "FDE roles in Berlin")
    response = client.post("/api/intent/condense/approve", json={"text": text})
    assert response.status_code == 422
    assert "text" in response.json()["detail"]["errors"]
    [entry] = client.get("/api/intent").json()["entries"]
    assert entry["kind"] == "entry"


def test_approve_accepts_exactly_the_limit(client: TestClient) -> None:
    seed(client, "FDE roles in Berlin")
    response = client.post("/api/intent/condense/approve", json={"text": "x" * MAX_CONDENSED_CHARS})
    assert response.status_code == 201
