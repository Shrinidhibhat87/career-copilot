"""The three things the curator can do, and what each of them refuses.

As in discovery, a cap answers the model with text instead of raising, so an agent that
hits one finishes with the shortlist it already built.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from career_copilot.domain.models import StoredJob
from career_copilot.usecases.rank import cosine

MAX_SEARCH_RESULTS = 20
JOB_PREVIEW_CHARS = 1200


@dataclass(slots=True)
class CuratorDeps:
    """The ranked pool and what the agent has picked from it. Never global state."""

    pool: Sequence[StoredJob]
    vectors: dict[int, list[float]]
    embed: Callable[[str], list[float]]
    limit: int
    shortlist: dict[int, str] = field(default_factory=dict)

    def job(self, job_id: int) -> StoredJob | None:
        return next((job for job in self.pool if job.id == job_id), None)


def line(job: StoredJob) -> str:
    p = job.posting
    return f"{job.id} | {p.title} | {p.company} | {p.location}"


def search_pool(deps: CuratorDeps, query: str, k: int = 10) -> str:
    """Pool jobs closest to `query`, best first, already-shortlisted ones marked."""
    target = deps.embed(query)
    scored = sorted(deps.pool, key=lambda job: (-cosine(deps.vectors[job.id], target), job.id))[
        : max(1, min(k, MAX_SEARCH_RESULTS))
    ]
    return "\n".join(
        line(job) + (" | shortlisted" if job.id in deps.shortlist else "") for job in scored
    )


def get_job(deps: CuratorDeps, job_id: int) -> str:
    job = deps.job(job_id)
    if job is None:
        return f"refused: job {job_id} is not in the pool"
    return f"{line(job)}\n{job.posting.description[:JOB_PREVIEW_CHARS]}"


def shortlist(deps: CuratorDeps, job_id: int, reason: str) -> str:
    if deps.job(job_id) is None:
        return f"refused: job {job_id} is not in the pool; use an id from search_pool"
    if job_id in deps.shortlist:
        return f"job {job_id} is already shortlisted"
    if len(deps.shortlist) >= deps.limit:
        return f"refused: the shortlist is full ({deps.limit}); stop and summarise"
    deps.shortlist[job_id] = reason.strip()
    return f"shortlisted {job_id} ({len(deps.shortlist)}/{deps.limit})"
