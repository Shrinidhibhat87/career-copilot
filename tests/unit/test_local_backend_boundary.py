"""A local model does not exempt a prompt from redaction.

The temptation is obvious: a call to a container on loopback cannot leak to a provider,
so why pay Presidio for it. The boundary is kept because the invariant is about the
prompt, not the destination — and the destination is one config line away from changing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import (
    BackendSpec,
    Config,
    ModelsSpec,
    PiiSpec,
    RoleSpec,
)
from career_copilot.ports import ResumeMissingError
from career_copilot.usecases.pipeline import build_score_run, current_preferences

# The composition is what is under test, but composing it builds the real detector, so an
# environment without the spaCy model skips exactly as the other Presidio tests do.
pytest.importorskip("en_core_web_lg")

LOCAL_ROLES = ModelsSpec(
    backends={"ollama": BackendSpec(kind="ollama", base_url="http://localhost:11434")},
    roles={
        "triage": RoleSpec(backend="ollama", model="qwen3:4b"),
        "match": RoleSpec(backend="ollama", model="qwen3:14b"),
    },
)


def local_config(tmp_path: Path) -> Config:
    return Config(db_path=str(tmp_path / "jobs.db"), models=LOCAL_ROLES)


def store_cv(config: Config) -> None:
    with SqliteStore(config.db_path) as store:
        store.save_cv("cv.pdf", "Data engineer. Python, Kafka, Databricks.", datetime.now(UTC))


def build(config: Config) -> object:
    with SqliteStore(config.db_path) as store:
        prefs = current_preferences(store, config)
        from career_copilot.usecases.pipeline import freeze_config

        frozen = freeze_config(config, store, prefs)
        return build_score_run(config, store, run_id=1, frozen=frozen)


def test_local_roles_are_still_wrapped_in_the_redaction_boundary(tmp_path: Path) -> None:
    config = local_config(tmp_path)
    store_cv(config)
    run = build(config)
    assert isinstance(run._triage._client, RedactingLLMClient)  # type: ignore[attr-defined]
    assert isinstance(run._match._client, RedactingLLMClient)  # type: ignore[attr-defined]


def test_the_frozen_run_names_the_local_model_that_will_answer(tmp_path: Path) -> None:
    config = local_config(tmp_path)
    with SqliteStore(config.db_path) as store:
        from career_copilot.usecases.pipeline import freeze_config

        frozen = freeze_config(config, store, current_preferences(store, config))
    assert frozen.triage_model == "ollama_chat/qwen3:4b"
    assert frozen.match_model == "ollama_chat/qwen3:14b"


def test_disabling_redaction_is_still_the_only_way_out(tmp_path: Path) -> None:
    config = local_config(tmp_path).model_copy(update={"pii": PiiSpec(enabled=False)})
    store_cv(config)
    run = build(config)
    assert not isinstance(run._triage._client, RedactingLLMClient)  # type: ignore[attr-defined]


def test_a_score_run_with_no_stored_cv_fails_clearly(tmp_path: Path) -> None:
    config = local_config(tmp_path)
    with pytest.raises(ResumeMissingError):
        build(config)


def test_the_run_scores_against_the_stored_cv(tmp_path: Path) -> None:
    config = local_config(tmp_path)
    store_cv(config)
    run = build(config)
    assert run._cv_sha256 is not None  # type: ignore[attr-defined]
    with SqliteStore(config.db_path) as store:
        cv = store.current_cv()
    assert cv is not None and cv.sha256 == run._cv_sha256  # type: ignore[attr-defined]
