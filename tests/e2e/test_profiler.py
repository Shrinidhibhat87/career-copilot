"""The profiler run, with a scripted model: caching, redaction and failure fallbacks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.profiler import build_profiler
from career_copilot.domain.sample_roles import SampleRole
from career_copilot.domain.search_profile import SearchProfile
from career_copilot.usecases.search_profile import ensure_profile

T = datetime(2026, 10, 7, tzinfo=UTC)
DRAFT = {
    "summary": "Wants forward-deployed engineering in applied AI, in Berlin.",
    "role_archetypes": ["forward deployed engineer"],
    "similar_companies": ["Taktile"],
    "search_phrases": ["forward deployed engineer"],
}


class Model:
    """Records every prompt it is sent and answers with the profile draft."""

    def __init__(self, fail: bool = False) -> None:
        self.prompts: list[str] = []
        self.fail = fail

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.prompts.append(str(messages))
        if self.fail:
            raise RuntimeError("backend down")
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, DRAFT)])

    def factory(self) -> Any:
        return build_profiler(FunctionModel(self.respond))


def redact(texts: Sequence[str]) -> Sequence[str]:
    return [text.replace("Marlena", "<|PERSON_1|>") for text in texts]


def seeded() -> SqliteStore:
    store = SqliteStore()
    store.save_cv("cv.txt", "Marlena. Built ML platforms.", T)
    store.add_intent("FDE roles in Berlin. Ask Marlena.", T)
    store.add_sample_role(
        SampleRole(0, "ashby", "taktile", "594e", "Taktile", "FDE", "Decision engines.", None, T),
        T,
    )
    return store


def run(store: SqliteStore, model: Model, **kw: Any) -> SearchProfile | None:
    return ensure_profile(store, model.factory, store.current_cv(), redact, "m", T, **kw)


def test_nothing_written_means_no_profile_and_no_model() -> None:
    model = Model()
    assert run(SqliteStore(), model) is None
    assert model.prompts == []


def test_profile_is_built_redacted_and_cached() -> None:
    store, model = seeded(), Model()
    profile = run(store, model)
    assert profile is not None and profile.similar_companies == ("Taktile",)
    assert len(model.prompts) == 1
    assert "Marlena" not in model.prompts[0]
    assert "<|PERSON_1|>" in model.prompts[0]
    assert "Decision engines." in model.prompts[0]
    assert run(store, model) == profile  # same inputs: the cache answers
    assert len(model.prompts) == 1


def test_a_new_entry_rebuilds_the_profile() -> None:
    store, model = seeded(), Model()
    run(store, model)
    store.add_intent("Also solutions engineering.", T)
    run(store, model)
    assert len(model.prompts) == 2


def test_a_failing_model_keeps_the_last_profile() -> None:
    store = seeded()
    first = run(store, Model())
    store.add_intent("Also solutions engineering.", T)
    assert run(store, Model(fail=True)) == first


def test_a_failing_model_with_no_history_gives_no_profile() -> None:
    assert run(seeded(), Model(fail=True)) is None


def test_a_run_scores_against_the_profile_it_refreshed() -> None:
    from career_copilot.config import Config
    from career_copilot.domain.preferences import Preferences
    from career_copilot.domain.runs import RunConfig
    from career_copilot.domain.search_profile import inputs_hash
    from career_copilot.usecases.pipeline import refresh_profile

    store = seeded()
    key = inputs_hash(store.active_intent(), store.active_sample_roles(), store.current_cv().sha256)  # type: ignore[union-attr]
    store.save_search_profile(key, SearchProfile("cached profile"), "m", T)
    frozen = RunConfig(Preferences(), "t", "m", 1, "initial", 1.0, 1, 0.02, 0.2, 256, 1024)
    refreshed = refresh_profile(Config(), store, frozen)
    assert refreshed.preferences.search_profile == "cached profile"
    assert refreshed.preferences_hash != frozen.preferences_hash
    assert refresh_profile(Config(), SqliteStore(), frozen) is frozen
