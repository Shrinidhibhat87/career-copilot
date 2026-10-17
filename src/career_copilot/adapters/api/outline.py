"""HTTP for a card's outline: the responsibilities and requirements of one posting.

Read on first open and kept, so a card costs one model call once. A failure is an
answer, not an error: the card then says the summary is unavailable and links to the
posting, and the next open tries again.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from career_copilot.adapters.llm.backends import build_client
from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.observability import recorder_for
from career_copilot.adapters.pii.factory import build_redactor
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.outline import OutlineAgent
from career_copilot.config import Config
from career_copilot.ports import LLMClient

log = logging.getLogger(__name__)


def default_client(config: Config) -> LLMClient:
    """The `match` model (the cheap `triage` budget is too small for quoted sentences),
    behind the same redaction boundary every provider call crosses."""
    client: LLMClient = build_client(config.role("match"), recorder_for(config))
    redactor = build_redactor(config)
    if redactor is not None:
        client = RedactingLLMClient(client, redactor, outbound=config.pii.outbound)
    return client


def router(
    get_config: Callable[[], Config],
    client_factory: Callable[[Config], LLMClient] = default_client,
) -> APIRouter:
    routes = APIRouter()

    # POST, not GET: a first open spends a model call.
    @routes.post("/api/jobs/{job_id}/outline")
    def outline(job_id: int) -> dict[str, object]:
        config = get_config()
        with SqliteStore(config.db_path) as store:
            job = store.get_job(job_id)
            if job is None:
                raise HTTPException(404, detail={"errors": {"jobId": "job not found"}})
            saved = store.get_outline(job_id)
        if saved is not None:
            return {"status": "ok", "responsibilities": saved[0], "requirements": saved[1]}
        if not job.posting.description:
            return {"status": "unavailable"}
        # The store is closed during the call: it can take seconds on a local model.
        try:
            result = OutlineAgent(client_factory(config)).outline(job.posting)
        except Exception:
            # A down backend, a bad answer and a refused call all land the same way for
            # the person reading: the summary is not there.
            log.warning("outline failed for job %s", job_id, exc_info=True)
            return {"status": "unavailable"}
        with SqliteStore(config.db_path) as store:
            store.save_outline(
                job_id,
                job.posting.content_hash,
                result.responsibilities,
                result.requirements,
                result.model,
                datetime.now(UTC),
            )
        return {
            "status": "ok",
            "responsibilities": result.responsibilities,
            "requirements": result.requirements,
        }

    return routes
