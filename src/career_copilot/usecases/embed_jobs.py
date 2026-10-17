"""Embed the pool once per posting text. Ranking reads vectors; only new text costs time."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import datetime

from career_copilot.domain.models import JobPosting, StoredJob
from career_copilot.ports import Embedder, JobVectorStore

# Texts per embedder call: large enough to amortise call overhead, small enough that a
# crash mid-pool loses little, since each batch is saved before the next starts.
BATCH = 64
MAX_DESCRIPTION_CHARS = 2000  # bge-small reads 512 tokens; the head of a posting is the role


def job_text(posting: JobPosting) -> str:
    """What the embedder reads: the identifying fields, then the head of the description."""
    return "\n".join(
        (
            posting.title,
            posting.company,
            posting.location,
            posting.description[:MAX_DESCRIPTION_CHARS],
        )
    )


def text_hash(text: str) -> str:
    """The cache key. Not `JobPosting.content_hash`: that covers the description alone, so
    two roles sharing a boilerplate description would share one vector, and a retitled
    posting would keep its old one."""
    return hashlib.sha256(text.encode()).hexdigest()


def ensure_job_vectors(
    store: JobVectorStore, embedder: Embedder, jobs: Sequence[StoredJob], now: datetime
) -> dict[int, list[float]]:
    """Vectors for every job, embedding only texts the cache has not seen under this model.

    An edited posting reads as new text and is re-embedded; two jobs with the same text
    share one embedding.
    """
    texts = {job.id: job_text(job.posting) for job in jobs}
    keys = {job_id: text_hash(text) for job_id, text in texts.items()}
    cached = store.job_vectors(embedder.model, keys.values())
    missing: dict[str, str] = {}
    for job_id, key in keys.items():
        if key not in cached:
            missing.setdefault(key, texts[job_id])
    pending = list(missing.items())
    for start in range(0, len(pending), BATCH):
        batch = pending[start : start + BATCH]
        vectors = embedder.embed([text for _, text in batch])
        batch_keys = [key for key, _ in batch]
        store.save_job_vectors(embedder.model, zip(batch_keys, vectors, strict=True), now)
        # Read back rather than keep the embedder's output: the store narrows to float32,
        # and a cold run must rank exactly like the warm runs that read the cache.
        cached.update(store.job_vectors(embedder.model, batch_keys))
    return {job_id: cached[key] for job_id, key in keys.items()}
