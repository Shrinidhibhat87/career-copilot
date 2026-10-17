"""A dismissal follows a repost.

A board that re-issues a posting gives it a new id and often a new URL, and a small title
or location change defeats the fingerprint, so the store sees a new job. This compares a
newly stored job with the dismissed jobs of the same company and, when the text is close,
dismisses it too. Local embeddings only: no model call, nothing leaves the machine.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from career_copilot.domain.models import StoredJob
from career_copilot.ports import Embedder, RepostStore
from career_copilot.usecases.embed_jobs import ensure_job_vectors
from career_copilot.usecases.rank import cosine

log = logging.getLogger(__name__)


def hide_reposts(
    store: RepostStore,
    embedder: Callable[[], Embedder],
    job_ids: Sequence[int],
    threshold: float,
    now: datetime,
) -> int:
    """Dismiss each new job that repeats a dismissed one. Returns how many were hidden.

    Fails open: if the embedder cannot load, the jobs stay visible and the run goes on.
    `embedder` is a factory, called at most once and only when a comparison is needed.
    """
    hidden = 0
    built: Embedder | None = None
    for job_id in job_ids:
        job = store.get_job(job_id)
        siblings = store.dismissed_siblings(job_id) if job is not None else ()
        if job is None or not siblings:
            continue
        try:
            if built is None:
                built = embedder()
            original = _closest_original(store, built, job, siblings, threshold, now)
        except Exception as exc:  # a missing model must not end discovery
            log.warning("repost check skipped for job %s: %s", job_id, exc)
            continue
        if original is not None:
            store.hide_as_repost(job_id, original, now)
            hidden += 1
    return hidden


def _closest_original(
    store: RepostStore,
    embedder: Embedder,
    job: StoredJob,
    siblings: Sequence[StoredJob],
    threshold: float,
    now: datetime,
) -> int | None:
    # A cleaned job has no description or vector left, so the only thing left to compare is
    # its title; a sibling that was cleaned matches on an identical title.
    live = [s for s in siblings if not store.is_cleaned(s.id)]
    for sibling in siblings:
        if sibling not in live and sibling.posting.title_norm == job.posting.title_norm:
            return sibling.id
    if not live:
        return None
    vectors = ensure_job_vectors(store, embedder, [job, *live], now)
    best_id, best = None, 0.0
    for sibling in live:
        similarity = cosine(vectors[job.id], vectors[sibling.id])
        if similarity > best:
            best_id, best = sibling.id, similarity
    return best_id if best >= threshold else None
