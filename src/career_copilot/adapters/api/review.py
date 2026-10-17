"""HTTP for the review screen: the ranked list, one card, and what the user says back.

A driving adapter only. The ranking, the cut and the append-only rule live in the store;
what belongs here is the mapping between HTTP and those calls, and refusing a label the
vocabulary does not contain before it reaches the database.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.company_resolution import careers_url, extract_ats_hint
from career_copilot.domain.lifecycle import DECISIONS, DISMISS_REASONS, LABEL_TO_DECISION

# The vocabulary the screen offers. Checked here rather than by a CHECK constraint so the
# refusal can name the field and list what is accepted.
LABELS: tuple[str, ...] = ("good_fit", "maybe", "not_relevant")
CORRECTION_KINDS: tuple[str, ...] = ("evidence", "rule")
# `restored` is written only by the recycle bin's restore, which checks the job is in the
# bin; accepted here it could bring back a job the user threw away.
USER_DECISIONS: tuple[str, ...] = tuple(d for d in DECISIONS if d != "restored")

# The list-view slider's range, from the UI spec. The default is the bottom of it; raising
# the bar is the user's move. Mirrored in review-screen.tsx.
# One screen never shows more than the review limit, so a larger batch is a client bug.
MAX_VIEWED_IDS = 200

MIN_THRESHOLD = 30
MAX_THRESHOLD = 90


def now() -> datetime:
    return datetime.now(UTC)


def _job_id(body: dict[str, Any]) -> int:
    value = body.get("jobId")
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, detail={"errors": {"jobId": "expected a job id"}})
    return value


def _text(body: dict[str, Any], key: str, limit: int = 2000) -> str:
    value = str(body.get(key, "")).strip()
    if len(value) > limit:
        raise HTTPException(422, detail={"errors": {key: f"keep it under {limit} characters"}})
    return value


def company_url(
    store: SqliteStore, company: str, source: str, job_url: str | None = None
) -> str | None:
    """Where a person goes to learn about the company: its imported homepage when the list
    had one, else the careers page of its board, else nothing.

    The board is read from the posting's own URL, since a job carries its vendor but not
    the slug; a company-list import that resolved a slug is the second source.
    """
    homepage = store.homepage_for_company(company)
    if homepage:
        return homepage
    hint = extract_ats_hint(job_url)
    if hint and hint[0] == source:
        return careers_url(*hint)
    slug = store.board_slug_for_company(company, source)
    return careers_url(source, slug) if slug else None


def router(get_config: Callable[[], Config]) -> APIRouter:
    routes = APIRouter()

    @routes.get("/api/review")
    def review(
        threshold: int = Query(MIN_THRESHOLD, ge=MIN_THRESHOLD, le=MAX_THRESHOLD),
        # camelCase on the wire, like every other payload this API serves.
        include_below: bool = Query(False, alias="includeBelow"),
        # Hard ceiling, enforced here: the screen steps 10 -> 25 -> 50 and never asks for more.
        limit: int = Query(10, ge=1, le=50),
        # The saved- and applied-jobs filters. Only one decision narrows the list;
        # dismissed is never listed, so it is not a value here.
        decision: str | None = Query(None, pattern="^(saved|maybe|applied)$"),
    ) -> dict[str, object]:
        config = get_config()
        with SqliteStore(config.db_path) as store:
            page = store.review_page(threshold, include_below, limit, decision)
            # For the "Recycle bin (n)" link.
            page["binCount"] = store.bin_count(now(), config.lifecycle.bin_days)
            items: list[dict[str, Any]] = page["items"]  # type: ignore[assignment]
            for item in items:
                item["companyUrl"] = company_url(
                    store, item["company"], item["source"], item["url"]
                )
        # The retirement horizon, so the screen can age a posting against the same number
        # the crawler uses instead of hardcoding one of its own.
        page["inactiveAfterDays"] = config.inactive_after_days
        return page

    @routes.get("/api/jobs/{job_id}")
    def job(job_id: int) -> dict[str, object]:
        with SqliteStore(get_config().db_path) as store:
            detail = store.job_detail(job_id)
            if detail is not None:
                detail["companyUrl"] = company_url(
                    store, str(detail["company"]), str(detail["source"]), str(detail["url"])
                )
        if detail is None:
            raise HTTPException(404, detail={"errors": {"jobId": "job not found"}})
        return detail

    @routes.post("/api/jobs/viewed")
    def viewed(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        ids = body.get("ids")
        if not isinstance(ids, list) or any(
            isinstance(item, bool) or not isinstance(item, int) for item in ids
        ):
            raise HTTPException(422, detail={"errors": {"ids": "expected a list of job ids"}})
        if len(ids) > MAX_VIEWED_IDS:
            raise HTTPException(
                422, detail={"errors": {"ids": f"at most {MAX_VIEWED_IDS} ids per request"}}
            )
        with SqliteStore(get_config().db_path) as store:
            marked = store.mark_viewed(ids, now())
        return {"marked": marked}

    @routes.post("/api/jobs/{job_id}/decision", status_code=201)
    def decide(job_id: int, body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        decision = str(body.get("decision", "")).strip()
        if decision not in USER_DECISIONS:
            raise HTTPException(
                422,
                detail={"errors": {"decision": f"expected one of {', '.join(USER_DECISIONS)}"}},
            )
        reason = body.get("reason")
        if reason is not None and (decision != "dismissed" or reason not in DISMISS_REASONS):
            raise HTTPException(
                422,
                detail={"errors": {"reason": f"expected one of {', '.join(DISMISS_REASONS)}"}},
            )
        with SqliteStore(get_config().db_path) as store:
            try:
                decision_id = store.record_decision(
                    job_id, decision, _text(body, "note"), now(), reason
                )
            except LookupError:
                raise HTTPException(404, detail={"errors": {"jobId": "job not found"}}) from None
        return {"decisionId": decision_id, "decision": decision}

    @routes.post("/api/jobs/{job_id}/undo-applied")
    def undo_applied(job_id: int) -> dict[str, object]:
        """Puts back the decision that stood before Applied. A Notion row is not touched."""
        with SqliteStore(get_config().db_path) as store:
            try:
                restored = store.undo_applied(job_id, now())
            except LookupError:
                raise HTTPException(404, detail={"errors": {"jobId": "job not found"}}) from None
            except ValueError:
                raise HTTPException(
                    409, detail={"errors": {"decision": "this job is not applied"}}
                ) from None
        return {"decision": restored}

    @routes.post("/api/reviews", status_code=201)
    def add_review(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        job_id = _job_id(body)
        label = str(body.get("label", "")).strip()
        if label not in LABELS:
            raise HTTPException(
                422, detail={"errors": {"label": f"expected one of {', '.join(LABELS)}"}}
            )
        with SqliteStore(get_config().db_path) as store:
            try:
                review_id = store.record_review(job_id, label, _text(body, "note"), now())
            except LookupError:
                raise HTTPException(404, detail={"errors": {"jobId": "job not found"}}) from None
        # A label now acts as a decision (dismiss/save/maybe), but it still does not re-rank
        # by learned preference: `rerankedNothing` is about that, and stays true.
        return {
            "reviewId": review_id,
            "label": label,
            "decision": LABEL_TO_DECISION[label],
            "rerankedNothing": True,
        }

    @routes.post("/api/corrections", status_code=201)
    def add_correction(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        job_id = _job_id(body)
        kind = str(body.get("kind", "")).strip()
        if kind not in CORRECTION_KINDS:
            raise HTTPException(
                422, detail={"errors": {"kind": f"expected one of {', '.join(CORRECTION_KINDS)}"}}
            )
        with SqliteStore(get_config().db_path) as store:
            try:
                correction_id = store.record_correction(
                    job_id, kind, _text(body, "target"), _text(body, "note"), now()
                )
            except LookupError:
                raise HTTPException(404, detail={"errors": {"jobId": "job not found"}}) from None
        return {"correctionId": correction_id, "rerankedNothing": True}

    return routes
