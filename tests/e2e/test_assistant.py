"""The feedback agent: what it does through its four tools, never how it words things."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from career_copilot.adapters.pii.redactor import Redaction
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.assistant import tools
from career_copilot.agents.assistant.agent import build_assistant
from career_copilot.agents.assistant.tools import AssistantDeps, Refresh, ViewState
from career_copilot.config import Config
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import FilterOutcome, ScoreStage
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from career_copilot.usecases.assistant import (
    FAILURE_TEXT,
    NO_ACTION_TEXT,
    Turn,
    assistant_context,
    converse,
)

NOW = datetime(2026, 10, 8, tzinfo=UTC)


class Script:
    """A scripted model: each turn is a list of tool calls or a final text. Records prompts."""

    def __init__(self, *turns: list[ToolCallPart] | str, fail: bool = False) -> None:
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.fail = fail

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.prompts.append(str(messages))
        if self.fail:
            raise RuntimeError("backend down")
        turn = self.turns.pop(0) if self.turns else "done"
        if isinstance(turn, str):
            return ModelResponse(parts=[TextPart(turn)])
        return ModelResponse(parts=list(turn))

    def agent(self) -> Any:
        return build_assistant(FunctionModel(self.respond))


@pytest.fixture
def config(tmp_path: Path) -> Config:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    SqliteStore(config.db_path).close()
    return config


def default_refresh(_config: Config) -> dict[str, object]:
    return {"started": True, "runId": 1}


def deps(config: Config, refresh: Refresh | None = None, limit: int = 10) -> AssistantDeps:
    return AssistantDeps(
        config=config,
        view=ViewState(limit=limit, include_below=False),
        refresh=refresh or default_refresh,
        now=lambda: NOW,
    )


def converse_with(
    config: Config, script: Script, message: str, history: Sequence[Turn] = (), **kw: Any
) -> Any:
    return converse(
        config,
        script.agent,
        message,
        list(history),
        kw.pop("view", ViewState(limit=10, include_below=False)),
        redact=kw.pop("redact", None),
        refresh=kw.pop("refresh", lambda _config: {"started": True, "runId": 1}),
        now=lambda: NOW,
        **kw,
    )


def saved(config: Config) -> list[tuple[str, str]]:
    with SqliteStore(config.db_path) as store:
        return [(e.role, e.text) for e in store.recent_feedback(50)]


def test_no_number_asks_first_and_changes_nothing(config: Config) -> None:
    script = Script(
        [ToolCallPart("view_counts", {})],
        "How many more would you like?",
    )
    reply = converse_with(config, script, "make it more")
    assert reply.actions == []
    assert "How many more" in reply.text


def test_ten_more_sets_the_view(config: Config) -> None:
    script = Script(
        [ToolCallPart("set_view", {"limit": 20, "include_below": True})],
        "Showing 20.",
    )
    reply = converse_with(config, script, "10 more", view=ViewState(limit=10, include_below=False))
    assert [a.model_dump(by_alias=True, exclude_none=True) for a in reply.actions] == [
        {"kind": "set_view", "limit": 20, "includeBelow": True}
    ]


def test_a_preference_is_stored_once_and_refreshes(config: Config) -> None:
    started: list[Config] = []

    def refresh(cfg: Config) -> dict[str, object]:
        started.append(cfg)
        return {"started": True, "runId": 3}

    script = Script([ToolCallPart("add_intent", {"text": "No fintech"})], "Noted.")
    reply = converse_with(config, script, "No fintech", refresh=refresh)
    with SqliteStore(config.db_path) as store:
        assert [e.text for e in store.active_intent()] == ["No fintech"]
    assert len(started) == 1
    assert [a.kind for a in reply.actions] == ["intent_added", "refresh"]


def test_look_for_new_jobs_calls_refresh(config: Config) -> None:
    script = Script([ToolCallPart("refresh_jobs", {})], "On it.")
    reply = converse_with(config, script, "Look for new jobs")
    assert [(a.kind, a.started) for a in reply.actions] == [("refresh", True)]


def test_a_started_search_carries_its_run_id(config: Config) -> None:
    script = Script([ToolCallPart("refresh_jobs", {})], "On it.")
    reply = converse_with(config, script, "Look for new jobs")
    assert [a.model_dump(by_alias=True, exclude_none=True) for a in reply.actions] == [
        {"kind": "refresh", "started": True, "runId": 1}
    ]


def test_a_run_in_progress_carries_the_reason(config: Config) -> None:
    script = Script([ToolCallPart("refresh_jobs", {})], "A run is already going.")
    reply = converse_with(
        config,
        script,
        "Look for new jobs",
        refresh=lambda _c: {"started": False, "reason": "queued after the active run"},
    )
    [action] = reply.actions
    assert (action.started, action.reason) == (False, "queued after the active run")


@pytest.mark.parametrize("limit", [0, -3])
def test_set_view_refuses_a_limit_below_one(config: Config, limit: int) -> None:
    d = deps(config)
    assert tools.set_view(d, limit, False).startswith("refused")
    assert d.actions == []


def test_set_view_clamps_a_huge_limit(config: Config) -> None:
    d = deps(config)
    assert not tools.set_view(d, 500, False).startswith("refused")
    assert d.actions[0].limit == 50


@pytest.mark.parametrize("text", ["", "   ", "x" * 2001])
def test_add_intent_refuses_empty_or_long_text(config: Config, text: str) -> None:
    d = deps(config)
    assert tools.add_intent(d, text).startswith("refused")
    assert d.actions == []
    with SqliteStore(config.db_path) as store:
        assert store.active_intent() == []


def test_both_messages_are_saved(config: Config) -> None:
    converse_with(config, Script("Hello."), "hi there")
    assert saved(config) == [("user", "hi there"), ("assistant", "Hello.")]


def test_a_failed_model_call_still_saves_the_users_message(config: Config) -> None:
    reply = converse_with(config, Script(fail=True), "hi there")
    assert reply.text == FAILURE_TEXT
    assert reply.actions == []
    assert saved(config) == [("user", "hi there"), ("assistant", FAILURE_TEXT)]


def test_context_is_redacted_and_never_holds_the_cv(config: Config) -> None:
    with SqliteStore(config.db_path) as store:
        store.add_intent("Marlena wants Berlin, mv@example.com, +49 170 1234567", NOW)
        store.save_cv("cv.txt", "CV BODY Marlena Voss Rust engineer", NOW)

    def redact(texts: Sequence[str]) -> Redaction:
        cleaned = [
            t.replace("Marlena", "<|PERSON_1|>")
            .replace("mv@example.com", "<|EMAIL_1|>")
            .replace("+49 170 1234567", "<|PHONE_1|>")
            for t in texts
        ]
        return Redaction(cleaned, {"PERSON": 1}, {"<|PERSON_1|>": "Marlena"})

    script = Script("ok")
    converse_with(config, script, "hello", redact=redact)
    prompt = script.prompts[0]
    assert "Marlena" not in prompt and "mv@example.com" not in prompt
    assert "1234567" not in prompt
    assert "CV BODY" not in prompt


def test_legacy_batch_corrections_are_read_as_earlier_feedback(config: Config) -> None:
    with SqliteStore(config.db_path) as store:
        store.record_correction(None, "batch", "", "too many agencies", NOW)
    with SqliteStore(config.db_path) as store:
        context = assistant_context(store, None)
    assert "too many agencies" in context.text


def seed_job(config: Config, n: int, title: str, score: int | None) -> int:
    """A posting the rules keep, scored at `score` (or not scored at all)."""
    from tests.conftest import make_raw

    with SqliteStore(config.db_path) as store:
        posting = to_posting(
            make_raw(source_job_id=str(n), url=f"https://x.test/{n}", title=title), {}
        )
        job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
        if score is not None:
            store.record_score(
                Score(
                    job_id=job_id,
                    stage=ScoreStage.MATCH,
                    score=score,
                    verdict=ScoreVerdict.from_score(score),
                    rubric_version=RUBRIC_VERSION,
                    model="m",
                    prompt_hash="p",
                    content_hash=posting.content_hash,
                ),
                NOW,
                cost_eur=0.0,
            )
    return job_id


def outcome_of(config: Config, job_id: int) -> tuple[FilterOutcome, str | None]:
    with SqliteStore(config.db_path) as store:
        row = store._conn.execute(
            "SELECT filter_outcome, (SELECT decision FROM job_decisions WHERE job_id = ?"
            " ORDER BY id DESC LIMIT 1) FROM jobs WHERE id = ?",
            (job_id, job_id),
        ).fetchone()
    return FilterOutcome(row[0]), row[1]


def test_exclude_roles_saves_the_words_filters_and_bins_the_listed_matches(
    config: Config,
) -> None:
    manager = seed_job(config, 1, "Engineering Manager", 70)
    engineer = seed_job(config, 2, "Data Engineer", 70)
    below_bar = seed_job(config, 3, "Data Manager", 10)
    script = Script([ToolCallPart("exclude_roles", {"terms": ["Manager"]})], "Done.")
    reply = converse_with(config, script, "remove manager positions")

    assert [a.model_dump(by_alias=True, exclude_none=True) for a in reply.actions] == [
        {"kind": "jobs_changed", "terms": ["manager"], "count": 1}
    ]
    assert outcome_of(config, manager) == (FilterOutcome.REJECT, "dismissed")
    assert outcome_of(config, engineer)[1] is None
    # Below the page's bar, so not listed: filtered by the new rule, but not binned.
    assert outcome_of(config, below_bar) == (FilterOutcome.REJECT, None)
    with SqliteStore(config.db_path) as store:
        page = store.review_page(threshold=30)
        listed = cast(list[dict[str, Any]], page["items"])
        assert [item["id"] for item in listed] == [engineer]
        current = store.load_current()
    assert current is not None
    assert "manager" in current.excluded_role_terms


def test_exclude_roles_never_touches_a_saved_job(config: Config) -> None:
    job_id = seed_job(config, 1, "Engineering Manager", 70)
    with SqliteStore(config.db_path) as store:
        store.record_decision(job_id, "saved", "", NOW)
    d = deps(config)
    assert tools.exclude_roles(d, ["manager"]).startswith("excluded")
    assert d.actions[0].count == 0
    assert outcome_of(config, job_id)[1] == "saved"


@pytest.mark.parametrize("terms", [[], ["  "], ["a", "b", "c", "d", "e", "f"], ["x" * 41]], ids=str)
def test_exclude_roles_refuses_bad_terms_and_changes_nothing(
    config: Config, terms: list[str]
) -> None:
    job_id = seed_job(config, 1, "Engineering Manager", 70)
    d = deps(config)
    assert tools.exclude_roles(d, terms).startswith("refused")
    assert d.actions == []
    assert outcome_of(config, job_id) == (FilterOutcome.MANUAL_REVIEW, None)


def test_exclude_roles_refuses_when_too_many_listed_postings_match(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "MAX_DISMISS", 1)
    first = seed_job(config, 1, "Engineering Manager", 70)
    seed_job(config, 2, "Data Manager", 70)
    d = deps(config)
    assert tools.exclude_roles(d, ["manager"]).startswith("refused")
    assert d.actions == []
    assert outcome_of(config, first) == (FilterOutcome.MANUAL_REVIEW, None)


def test_a_claimed_change_with_no_tool_call_is_retried_then_made_honest(config: Config) -> None:
    script = Script("I've removed all manager roles.", "I've removed all manager roles.")
    reply = converse_with(config, script, "remove manager roles")
    assert reply.text == NO_ACTION_TEXT
    assert reply.actions == []
    assert len(script.prompts) == 2


def test_a_retry_that_calls_the_tool_keeps_the_real_answer(config: Config) -> None:
    seed_job(config, 1, "Engineering Manager", 70)
    script = Script(
        "I've removed all manager roles.",
        [ToolCallPart("exclude_roles", {"terms": ["manager"]})],
        "Removed 1 manager role.",
    )
    reply = converse_with(config, script, "remove manager roles")
    assert reply.text == "Removed 1 manager role."
    assert [a.kind for a in reply.actions] == ["jobs_changed"]


def test_a_plain_answer_is_not_retried(config: Config) -> None:
    script = Script("Above the bar means a match of 30 or more.")
    reply = converse_with(config, script, "what does the bar mean?")
    assert reply.text.startswith("Above the bar")
    assert len(script.prompts) == 1


def test_only_replies_that_acted_go_back_into_the_prompt(config: Config) -> None:
    with SqliteStore(config.db_path) as store:
        store.add_feedback("user", "remove manager roles", None, NOW)
        store.add_feedback("assistant", "I've removed them all.", None, NOW)
        store.add_feedback("assistant", "Excluded manager.", '[{"kind": "jobs_changed"}]', NOW)
    script = Script("ok")
    converse_with(config, script, "hello")
    assert "I've removed them all." not in script.prompts[0]
    assert "Excluded manager." in script.prompts[0]
