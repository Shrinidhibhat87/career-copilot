"""Compare what two backends recommended: vLLM on the 1660 Ti against Ollama on the Mac.

Each machine exports its own top list to JSON, and the two files are compared offline,
so neither machine needs to reach the other. Jobs are matched by canonical URL, which is
the same on both machines when they crawl the same boards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from career_copilot.domain.scoring import ScoredJob, rank


def export_top(scored: Sequence[ScoredJob], now: datetime, limit: int = 50) -> dict[str, Any]:
    """The current top `limit` matches with their reasons, best first."""
    top = rank(scored)[:limit]
    return {
        "exportedAt": now.isoformat(),
        "models": sorted({item.score.model for item in top}),
        "jobs": [
            {
                "url": item.job.posting.canonical_url,
                "title": item.job.posting.title,
                "company": item.job.posting.company,
                "score": item.score.score,
                "verdict": str(item.score.verdict),
                "reasons": list(item.score.reasons),
                "model": item.score.model,
            }
            for item in top
        ],
    }


@dataclass(frozen=True, slots=True)
class Comparison:
    size_a: int
    size_b: int
    shared: int
    only_a: tuple[str, ...]
    only_b: tuple[str, ...]
    # Spearman's rho over the shared jobs' ranks; None with fewer than two shared.
    rank_correlation: float | None
    mean_score_delta: float | None

    @property
    def jaccard(self) -> float:
        union = self.size_a + self.size_b - self.shared
        return self.shared / union if union else 1.0


def _spearman(a: Sequence[str], b: Sequence[str]) -> float | None:
    """Rank correlation of the shared items, re-ranked within the shared set."""
    shared = [url for url in a if url in set(b)]
    n = len(shared)
    if n < 2:
        return None
    rank_b = {url: i for i, url in enumerate(url for url in b if url in set(shared))}
    d2 = sum((i - rank_b[url]) ** 2 for i, url in enumerate(shared))
    return 1 - 6 * d2 / (n * (n * n - 1))


def compare(a: Mapping[str, Any], b: Mapping[str, Any]) -> Comparison:
    """Overlap, order agreement and score drift between two exports."""
    urls_a = [job["url"] for job in a["jobs"]]
    urls_b = [job["url"] for job in b["jobs"]]
    score_a = {job["url"]: job["score"] for job in a["jobs"]}
    score_b = {job["url"]: job["score"] for job in b["jobs"]}
    shared = [url for url in urls_a if url in score_b]
    deltas = [abs(score_a[url] - score_b[url]) for url in shared]
    return Comparison(
        size_a=len(urls_a),
        size_b=len(urls_b),
        shared=len(shared),
        only_a=tuple(url for url in urls_a if url not in score_b),
        only_b=tuple(url for url in urls_b if url not in score_a),
        rank_correlation=_spearman(urls_a, urls_b),
        mean_score_delta=sum(deltas) / len(deltas) if deltas else None,
    )
