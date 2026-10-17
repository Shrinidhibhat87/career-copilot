"""Order the scoring candidates: embedding rank, then the curator's shortlist on top.

Replaces "triage every job with an LLM". Every step can fail without failing the run: no
references or a broken embedder hand back `None` and the run falls back to LLM triage; a
broken curator leaves the pure embedding order.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic_ai import Agent

from career_copilot.agents.curator.agent import (
    CuratorLimits,
    curator_prompt,
    usage_limits,
)
from career_copilot.agents.curator.tools import CuratorDeps
from career_copilot.agents.recording import AgentRecording
from career_copilot.domain.models import StoredJob
from career_copilot.ports import Embedder, IntentStore, JobVectorStore, SampleRoleStore
from career_copilot.usecases.embed_jobs import ensure_job_vectors
from career_copilot.usecases.rank import rank

log = logging.getLogger(__name__)

# How many of the best-ranked candidates the curator may look at. A 4B model reads
# titles, not hundreds of postings, so the pool is bounded rather than the whole crawl.
POOL_SIZE = 150
# The curator's shortlist is a head start, not the whole day: past this the embedding
# order fills in, which keeps a 30-tool-call budget enough to finish.
MAX_SHORTLIST = 20
MAX_REFERENCE_CHARS = 2000


class RankSources(IntentStore, SampleRoleStore, JobVectorStore, Protocol):
    """What the ranker reads references from and caches vectors in."""


@dataclass(slots=True)
class EmbeddingRanker:
    store: RankSources
    embedder: Callable[[], Embedder]
    clock: Callable[[], datetime]
    profile: str = ""
    # The current CV's text, or "" when none is stored. Only its head is embedded.
    cv_text: str = ""
    cv_weight: float = 0.0
    curator: Callable[[], Agent[CuratorDeps, str]] | None = None
    limits: CuratorLimits = field(default_factory=CuratorLimits)
    recording: AgentRecording | None = None
    # Filled by a call, for the run summary.
    shortlisted: int = 0

    def __call__(self, candidates: Sequence[StoredJob], target: int) -> list[StoredJob] | None:
        exemplars = [
            f"{r.title}\n{r.company}\n{r.text[:MAX_REFERENCE_CHARS]}"
            for r in self.store.active_sample_roles()
        ]
        intents = [entry.text for entry in self.store.active_intent()]
        if self.profile.strip():
            intents.append(self.profile)
        if not exemplars and not intents:
            return None
        try:
            embedder = self.embedder()
            vectors = ensure_job_vectors(self.store, embedder, candidates, self.clock())
            cv_vector = (
                embedder.embed([self.cv_text[:MAX_REFERENCE_CHARS]])[0]
                if self.cv_text.strip() and self.cv_weight > 0
                else None
            )
            order = rank(
                vectors,
                embedder.embed(exemplars) if exemplars else [],
                embedder.embed(intents) if intents else [],
                cv=cv_vector,
                cv_weight=self.cv_weight,
            )
        except Exception as exc:
            # A missing model download or a broken ONNX runtime: triage still works.
            log.warning("embedding ranking failed, falling back to triage: %s", exc)
            return None
        by_id = {job.id: job for job in candidates}
        ranked = [by_id[job_id] for job_id, _ in order]
        picked = self._curate(ranked[:POOL_SIZE], vectors, embedder, target)
        chosen = set(picked)
        return [by_id[i] for i in picked] + [job for job in ranked if job.id not in chosen]

    def _curate(
        self,
        pool: list[StoredJob],
        vectors: dict[int, list[float]],
        embedder: Embedder,
        target: int,
    ) -> list[int]:
        """Shortlisted ids in the order picked; empty when there is no curator or it failed.

        Whatever was shortlisted before a failure is kept: those picks were real.
        """
        if self.curator is None or not pool:
            return []
        deps = CuratorDeps(
            pool=pool,
            vectors=vectors,
            embed=lambda text: embedder.embed([text])[0],
            limit=max(1, min(target, MAX_SHORTLIST)),
        )
        if self.recording is not None:
            self.recording.start()
        result = None
        outcome = "ok"
        try:
            agent = self.curator()
            result = asyncio.run(
                asyncio.wait_for(
                    agent.run(
                        curator_prompt(self.profile, deps),
                        deps=deps,
                        usage_limits=usage_limits(self.limits),
                    ),
                    timeout=self.limits.max_seconds,
                )
            )
        except Exception as exc:
            # Deliberately broad: a 4B model must not be able to break a run.
            outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
            log.warning("curator stopped early, keeping %d picks: %s", len(deps.shortlist), exc)
        if self.recording is not None:
            self.recording.finish(result, outcome)
        self.shortlisted = len(deps.shortlist)
        return list(deps.shortlist)
