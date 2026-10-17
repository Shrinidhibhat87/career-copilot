"""Export a reproducible, company-capped, relevance-stratified labeling set."""

from __future__ import annotations

import argparse
import csv
import random
from collections import Counter, defaultdict, deque
from pathlib import Path

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.scoring import ScoredJob

FIELDS = ["job_id", "company", "title", "location", "url", "description", "label"]
BANDS = ("high", "mid", "low")


def relevance_band(score: int) -> str:
    return "high" if score >= 70 else "mid" if score >= 40 else "low"


def sample_calibration_set(
    candidates: list[ScoredJob], size: int, max_per_company: int, seed: int = 0
) -> list[ScoredJob]:
    rng = random.Random(seed)
    grouped: dict[str, dict[str, list[ScoredJob]]] = {band: defaultdict(list) for band in BANDS}
    for candidate in candidates:
        band = relevance_band(candidate.score.score)
        grouped[band][candidate.job.posting.company_norm].append(candidate)

    queues: dict[str, deque[ScoredJob]] = {}
    for band in BANDS:
        companies = list(grouped[band])
        rng.shuffle(companies)
        companies.sort(key=lambda name: len(grouped[band][name]))
        for name in companies:
            rng.shuffle(grouped[band][name])
        queue: deque[ScoredJob] = deque()
        while companies:
            remaining: list[str] = []
            for name in companies:
                queue.append(grouped[band][name].pop())
                if grouped[band][name]:
                    remaining.append(name)
            companies = remaining
        queues[band] = queue

    counts: Counter[str] = Counter()
    selected: list[ScoredJob] = []

    def take(band: str) -> bool:
        while queues[band]:
            item = queues[band].popleft()
            company = item.job.posting.company_norm
            if counts[company] >= max_per_company:
                continue
            selected.append(item)
            counts[company] += 1
            return True
        return False

    base, extra = divmod(size, len(BANDS))
    targets = {band: base + int(index < extra) for index, band in enumerate(BANDS)}
    for band in BANDS:
        for _ in range(targets[band]):
            if not take(band):
                break
    while len(selected) < size and any(queues.values()):
        if not any(take(band) for band in BANDS if len(selected) < size):
            break
    return selected


def export_calibration_set(
    db_path: str,
    out_path: str | Path,
    size: int = 25,
    seed: int = 0,
    max_per_company: int = 2,
) -> tuple[int, Counter[str]]:
    with SqliteStore(db_path) as store:
        candidates = list(store.triage_scores())
    if not candidates:
        raise RuntimeError("no current triage scores; run `career-copilot score` first")
    jobs = sample_calibration_set(candidates, size, max_per_company, seed)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for item in jobs:
            posting = item.job.posting
            writer.writerow(
                {
                    "job_id": item.job.id,
                    "company": posting.company,
                    "title": posting.title,
                    "location": posting.location,
                    "url": posting.canonical_url,
                    "description": " ".join(posting.description.split())[:2000],
                    "label": "",
                }
            )
    return len(jobs), Counter(relevance_band(item.score.score) for item in jobs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--out", default="tests/calibration/labels.csv")
    parser.add_argument("--size", type=int, default=25)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    config = Config.load(args.config)
    try:
        count, bands = export_calibration_set(
            config.db_path, args.out, args.size, args.seed, config.filters.max_per_company
        )
    except RuntimeError as exc:
        parser.error(str(exc))
    message = f"wrote {count}/{args.size} rows to {args.out}; bands: " + ", ".join(
        f"{band}={bands[band]}" for band in BANDS
    )
    if count < args.size:
        message += (
            f"\nshort by {args.size - count}: the per-company cap of "
            f"{config.filters.max_per_company} needs at least "
            f"{-(-args.size // config.filters.max_per_company)} scored companies"
        )
    print(message)


if __name__ == "__main__":
    main()
