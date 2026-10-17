"""Calibration against a human-labeled set. Opt-in: needs a live model.

Run with: uv run pytest -m calibration
"""

from __future__ import annotations

import csv
import os
from pathlib import Path

import pytest
from dotenv import load_dotenv

from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.adapters.profile_loader import load_notes
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.match import MatchAgent
from career_copilot.config import Config
from career_copilot.domain.models import RawJob
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.profile import Profile

LABELS = Path(__file__).parent / "labels.csv"
LABEL_RANK = {"no": 0, "ok": 1, "strong": 2}

pytestmark = pytest.mark.calibration


def load_labeled() -> list[tuple[RawJob, int]]:
    if not LABELS.is_file():
        pytest.skip(f"no labeled set at {LABELS}; run scripts/export_calibration_set.py first")
    rows = list(csv.DictReader(LABELS.open(encoding="utf-8")))
    labeled = [r for r in rows if (r.get("label") or "").strip().lower() in LABEL_RANK]
    if len(labeled) < 10:
        pytest.skip(f"only {len(labeled)} labeled rows; need at least 10")
    return [
        (
            RawJob(
                source="calibration",
                source_job_id=row["job_id"],
                company=row["company"],
                title=row["title"],
                location=row["location"],
                url=row["url"],
                description=row["description"],
            ),
            LABEL_RANK[row["label"].strip().lower()],
        )
        for row in labeled
    ]


def spearman(a: list[float], b: list[float]) -> float:
    """Rank correlation. Written out because the domain is stdlib-only."""

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for position, index in enumerate(order):
            out[index] = float(position)
        return out

    ra, rb = ranks(a), ranks(b)
    n = len(a)
    mean_a, mean_b = sum(ra) / n, sum(rb) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(ra, rb, strict=True))
    var_a = sum((x - mean_a) ** 2 for x in ra) ** 0.5
    var_b = sum((y - mean_b) ** 2 for y in rb) ** 0.5
    return cov / (var_a * var_b) if var_a and var_b else 0.0


@pytest.fixture(scope="module")
def scores() -> list[tuple[int, int]]:
    config = Config.load()
    if not config.scoring.match_model:
        pytest.skip("scoring.match_model is not configured")
    load_dotenv()
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY is not set; calibration needs a live model")
    with SqliteStore(config.db_path) as store:
        cv = store.current_cv()
    if cv is None:
        pytest.skip("no CV stored; upload one on the CV page")
    profile = Profile(preferences=load_notes(config.profile_dir), resume=cv.text)
    agent = MatchAgent(LiteLLMClient(model=config.scoring.match_model), profile)
    return [(agent.evaluate(to_posting(raw)).score, label) for raw, label in load_labeled()]


def test_scores_correlate_with_the_labels(scores: list[tuple[int, int]]) -> None:
    correlation = spearman([float(s) for s, _ in scores], [float(rank) for _, rank in scores])
    assert correlation >= 0.5, f"rank correlation {correlation:.2f} is too weak"


def test_nothing_labeled_no_scores_in_the_top_band(scores: list[tuple[int, int]]) -> None:
    high_but_rejected = [s for s, label in scores if s >= 70 and label == 0]
    assert not high_but_rejected, f"jobs labeled 'no' scored {high_but_rejected}"


def test_strong_labels_outscore_no_labels_on_average(scores: list[tuple[int, int]]) -> None:
    strong = [s for s, label in scores if label == 2]
    weak = [s for s, label in scores if label == 0]
    if not strong or not weak:
        pytest.skip("need both strong and no labels")
    assert sum(strong) / len(strong) > sum(weak) / len(weak)
