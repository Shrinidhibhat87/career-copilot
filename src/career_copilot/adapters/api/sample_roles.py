"""HTTP for sample roles: postings the user holds up as "more like this".

A link is resolved to the posting it names and its company's board joins the crawl. A link
that cannot be used asks for pasted text instead (`needsText`), and nothing is stored.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Response

from career_copilot.adapters.sources.ats import Fetcher, http_fetcher
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.sample_roles import SampleRole
from career_copilot.usecases.sample_roles import SampleRoleError, resolve_job_url

# A long posting is a few thousand characters. The cap only stops a pasted page dump
# from bloating every prompt the agent later builds from these.
MAX_TEXT_CHARS = 20_000


def now() -> datetime:
    return datetime.now(UTC)


def role_payload(role: SampleRole) -> dict[str, object]:
    return {
        "id": role.id,
        "company": role.company,
        "title": role.title,
        "source": role.source,
        "url": role.url,
        "addedAt": role.added_at.isoformat(),
    }


def _field(body: dict[str, Any], key: str, limit: int) -> str:
    value = body.get(key)
    return value.strip()[:limit] if isinstance(value, str) else ""


def router(
    get_config: Callable[[], Config],
    fetcher_factory: Callable[[], Fetcher] = http_fetcher,
    on_change: Callable[[Config], object] | None = None,
) -> APIRouter:
    routes = APIRouter()

    def changed(tasks: BackgroundTasks) -> None:
        # After the response: the refresh probes the model backend, which the user saving
        # a sentence should never wait for.
        if on_change is not None:
            tasks.add_task(on_change, get_config())

    @routes.get("/api/sample-roles")
    def list_roles() -> dict[str, object]:
        with SqliteStore(get_config().db_path) as store:
            roles = store.active_sample_roles()
        return {"roles": [role_payload(r) for r in roles]}

    @routes.post("/api/sample-roles", status_code=201)
    def add_role(
        body: Annotated[dict[str, Any], Body()], response: Response, tasks: BackgroundTasks
    ) -> dict[str, object]:
        url = _field(body, "url", 2000)
        stamp = now()
        if url:
            try:
                board, slug, job = resolve_job_url(url, fetcher_factory())
            except SampleRoleError as error:
                raise HTTPException(
                    422, detail={"errors": {"url": str(error)}, "needsText": True}
                ) from error
            candidate = SampleRole(
                id=0,
                source=board,
                slug=slug,
                source_job_id=job.source_job_id,
                company=job.company,
                title=job.title,
                text=job.description[:MAX_TEXT_CHARS],
                url=url,
                added_at=stamp,
            )
        else:
            title, text = _field(body, "title", 300), _field(body, "text", MAX_TEXT_CHARS)
            errors = {
                key: "required" for key, value in (("title", title), ("text", text)) if not value
            }
            if errors:
                raise HTTPException(422, detail={"errors": errors})
            candidate = SampleRole(
                id=0,
                source="manual",
                slug=None,
                source_job_id=None,
                company=_field(body, "company", 200) or "unknown",
                title=title,
                text=text,
                url=None,
                added_at=stamp,
            )
        # One transaction: the role and the board it puts in the crawl land together, and
        # two racing adds of one posting serialize on the write lock instead of tripping
        # the unique index.
        with SqliteStore(get_config().db_path) as store, store.transaction():
            role, created = store.add_sample_role(candidate, stamp)
            if created and candidate.slug is not None:
                store.add_sample_role_target(candidate.source, candidate.slug, stamp)
        if not created:
            response.status_code = 200
        else:
            changed(tasks)
        return role_payload(role)

    @routes.delete("/api/sample-roles/{role_id}", status_code=204)
    def archive_role(role_id: int, tasks: BackgroundTasks) -> Response:
        with SqliteStore(get_config().db_path) as store:
            archived = store.archive_sample_role(role_id, now())
        if not archived:
            raise HTTPException(404, detail={"errors": {"id": "no such sample role"}})
        changed(tasks)
        return Response(status_code=204)

    return routes
