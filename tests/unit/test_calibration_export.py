from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scripts.export_calibration_set import export_calibration_set

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import ScoreStage
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.scoring import RUBRIC_VERSION, Score, ScoreVerdict
from tests.conftest import make_raw

NOW = datetime(2026, 8, 12, tzinfo=UTC)


def test_export_is_seeded_stratified_and_company_capped(tmp_path: Path) -> None:
    db = tmp_path / "jobs.db"
    with SqliteStore(db) as store:
        for index, relevance in enumerate([90, 80, 60, 50, 30, 20]):
            company = f"Company {index // 2} GmbH"
            posting = to_posting(
                make_raw(
                    source_job_id=str(index),
                    company=company,
                    title=f"Python Engineer {index}",
                    url=f"https://example.com/{index}",
                )
            )
            job_id, _ = store.upsert_job(posting, apply_hard_filters(posting), NOW)
            store.record_score(
                Score(
                    job_id=job_id,
                    stage=ScoreStage.TRIAGE,
                    score=relevance,
                    verdict=ScoreVerdict.from_score(relevance),
                    rubric_version=RUBRIC_VERSION,
                    model="triage",
                    prompt_hash="prompt",
                    content_hash=posting.content_hash,
                ),
                NOW,
            )
    first = tmp_path / "first.csv"
    second = tmp_path / "second.csv"
    count, bands = export_calibration_set(str(db), first, 6, seed=7, max_per_company=2)
    export_calibration_set(str(db), second, 6, seed=7, max_per_company=2)
    assert count == 6
    assert first.read_bytes() == second.read_bytes()
    assert bands == Counter({"high": 2, "mid": 2, "low": 2})
    companies = [line.split(",")[1] for line in first.read_text().splitlines()[1:]]
    assert max(Counter(companies).values()) <= 2
