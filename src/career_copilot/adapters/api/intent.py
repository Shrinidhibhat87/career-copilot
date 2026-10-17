"""HTTP for the intent log: what the user is looking for, one entry at a time.

A driving adapter only. Entries are append-only in the store; a DELETE archives, so the
words the agent once learned from are never rewritten. Condensing is two calls: one drafts
a note for the user to edit, the other swaps the approved note in for the entries.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Response
from pydantic_ai import Agent

from career_copilot.adapters.observability import agent_recording
from career_copilot.adapters.pii.factory import build_redactor
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.condenser import build_condenser
from career_copilot.agents.discovery.agent import build_model
from career_copilot.config import Config
from career_copilot.domain.intent import (
    CONDENSE_AT_WORDS,
    MAX_ENTRY_CHARS,
    IntentEntry,
    NothingToCondenseError,
    active_words,
)
from career_copilot.usecases.condense_intent import (
    InvalidCondensedError,
    Redact,
    approve_condensed,
    draft_condensed,
)


def now() -> datetime:
    return datetime.now(UTC)


def entry_payload(entry: IntentEntry) -> dict[str, object]:
    """camelCase on the wire, like every other payload this API serves."""
    return {
        "id": entry.id,
        "text": entry.text,
        "kind": entry.kind.value,
        "createdAt": entry.created_at.isoformat(),
    }


def log_payload(entries: list[IntentEntry]) -> dict[str, object]:
    # `condenseAt` travels with the count so the screen never hard-codes the threshold.
    return {
        "entries": [entry_payload(e) for e in entries],
        "activeWords": active_words(entries),
        "condenseAt": CONDENSE_AT_WORDS,
    }


def default_condenser(config: Config) -> Agent[None, str]:
    """Condensing is the profiler's kind of judgement, so it runs on the profiler's model."""
    return build_condenser(build_model(config.role("profiler")))


def default_redact(config: Config) -> Redact | None:
    redactor = build_redactor(config)
    return redactor.redact if redactor is not None else None


def router(
    get_config: Callable[[], Config],
    condenser_factory: Callable[[Config], Agent[None, str]] = default_condenser,
    redact_factory: Callable[[Config], Redact | None] = default_redact,
    on_change: Callable[[Config], object] | None = None,
) -> APIRouter:
    routes = APIRouter()

    def changed(tasks: BackgroundTasks) -> None:
        # After the response: the refresh probes the model backend, which the user saving
        # a sentence should never wait for.
        if on_change is not None:
            tasks.add_task(on_change, get_config())

    @routes.get("/api/intent")
    def list_intent() -> dict[str, object]:
        with SqliteStore(get_config().db_path) as store:
            entries = store.active_intent()
        return log_payload(entries)

    @routes.post("/api/intent", status_code=201)
    def add_intent(
        body: Annotated[dict[str, Any], Body()], tasks: BackgroundTasks
    ) -> dict[str, object]:
        raw = body.get("text")
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            raise HTTPException(422, detail={"errors": {"text": "write what you are looking for"}})
        if len(text) > MAX_ENTRY_CHARS:
            raise HTTPException(
                422,
                detail={"errors": {"text": f"keep one entry under {MAX_ENTRY_CHARS} characters"}},
            )
        with SqliteStore(get_config().db_path) as store:
            entry = store.add_intent(text, now())
        changed(tasks)
        return entry_payload(entry)

    @routes.post("/api/intent/condense")
    def condense() -> dict[str, object]:
        config = get_config()
        with SqliteStore(config.db_path) as store:
            try:
                # Inside the try: an unreachable PII recall model fails here, at build time,
                # and the user who asked for a draft should hear why.
                redact = redact_factory(config)
                draft = draft_condensed(
                    store,
                    lambda: condenser_factory(config),
                    redact,
                    recording=agent_recording(config, config.role("profiler"), "condenser"),
                )
            except NothingToCondenseError as exc:
                raise HTTPException(422, detail={"errors": {"text": str(exc)}}) from exc
            except Exception as exc:
                # The draft is user-initiated, so a failure is surfaced, not swallowed.
                reason = str(exc) or type(exc).__name__
                raise HTTPException(
                    502, detail={"errors": {"model": f"Could not draft a condensed note: {reason}"}}
                ) from exc
        return {"draft": draft}

    @routes.post("/api/intent/condense/approve", status_code=201)
    def approve(
        body: Annotated[dict[str, Any], Body()], tasks: BackgroundTasks
    ) -> dict[str, object]:
        raw = body.get("text")
        with SqliteStore(get_config().db_path) as store:
            try:
                approve_condensed(store, raw if isinstance(raw, str) else "", now())
            except InvalidCondensedError as exc:
                raise HTTPException(422, detail={"errors": {"text": str(exc)}}) from exc
            entries = store.active_intent()
        changed(tasks)
        return log_payload(entries)

    @routes.delete("/api/intent/{entry_id}", status_code=204)
    def archive_intent(entry_id: int, tasks: BackgroundTasks) -> Response:
        with SqliteStore(get_config().db_path) as store:
            archived = store.archive_intent(entry_id, now())
        if not archived:
            raise HTTPException(404, detail={"errors": {"id": "no such entry"}})
        changed(tasks)
        return Response(status_code=204)

    return routes
