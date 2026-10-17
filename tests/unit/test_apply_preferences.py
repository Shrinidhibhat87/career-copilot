"""Saving preferences re-filters the jobs already stored. No model call, no score row."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.domain.filters import apply_hard_filters
from career_copilot.domain.models import FilterOutcome
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.preferences import LanguageSkill, Preferences
from career_copilot.usecases.apply_preferences import apply_preferences, plan_refilter
from tests.conftest import make_raw

MODEL = "m"
RUBRIC = 1


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as opened:
        yield opened


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def prefs(**overrides: object) -> Preferences:
    defaults: dict[str, object] = {
        "core_skills": ("python", "agentic"),
        "allowed_cities": ("berlin", "remote", ""),
        "languages": (LanguageSkill("German", "B1"),),
    }
    defaults.update(overrides)
    return Preferences(**defaults)  # type: ignore[arg-type]


def seed(store: SqliteStore, now: datetime, config: Preferences, **overrides: object) -> int:
    posting = to_posting(make_raw(**overrides))
    verdict = apply_hard_filters(posting, config.to_filter_config())
    job_id, _ = store.upsert_job(posting, verdict, now)
    return job_id


def outcome_of(store: SqliteStore, job_id: int) -> str:
    row = store._conn.execute("SELECT filter_outcome FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return str(row["filter_outcome"])


def test_narrowing_the_city_list_moves_a_stored_job_to_rejected(
    store: SqliteStore, now: datetime
) -> None:
    """The outcome is stored on the row at discover time, so without this it never moves."""
    job_id = seed(store, now, prefs(), location="Berlin, Germany")
    assert outcome_of(store, job_id) == FilterOutcome.PASS

    report = apply_preferences(store, prefs(allowed_cities=("hamburg",)), MODEL, RUBRIC)

    assert outcome_of(store, job_id) == FilterOutcome.REJECT
    assert report.changed == 1
    assert report.transitions == {"pass→reject": 1}


def test_widening_the_city_list_brings_a_job_back(store: SqliteStore, now: datetime) -> None:
    narrow = prefs(allowed_cities=("hamburg",))
    job_id = seed(store, now, narrow, location="Berlin, Germany")
    assert outcome_of(store, job_id) == FilterOutcome.REJECT

    report = apply_preferences(store, prefs(), MODEL, RUBRIC)

    assert outcome_of(store, job_id) == FilterOutcome.PASS
    assert report.now_passing == 1


def test_an_unchanged_job_is_examined_but_not_rewritten(store: SqliteStore, now: datetime) -> None:
    seed(store, now, prefs())
    report = apply_preferences(store, prefs(), MODEL, RUBRIC)
    assert (report.examined, report.changed, report.transitions) == (1, 0, {})


def test_the_re_filter_writes_no_score_row(store: SqliteStore, now: datetime) -> None:
    seed(store, now, prefs())
    apply_preferences(store, prefs(allowed_cities=("hamburg",)), MODEL, RUBRIC)
    assert store._conn.execute("SELECT COUNT(*) n FROM scores").fetchone()["n"] == 0


def test_it_reports_how_many_jobs_the_next_run_would_re_open(
    store: SqliteStore, now: datetime
) -> None:
    seed(store, now, prefs(), source_job_id="1", url="https://x/1")
    seed(
        store,
        now,
        prefs(),
        source_job_id="2",
        url="https://x/2",
        title="Senior Agentic AI Engineer",
    )
    report = apply_preferences(store, prefs(), MODEL, RUBRIC)
    assert report.reopened_for_scoring == 2


def test_a_seniority_band_change_reopens_a_previously_rejected_job(
    store: SqliteStore, now: datetime
) -> None:
    job_id = seed(store, now, prefs(), title="Staff Agentic AI Engineer")
    assert outcome_of(store, job_id) == FilterOutcome.REJECT

    widened = prefs(seniority=("mid", "senior", "staff"))
    report = apply_preferences(store, widened, MODEL, RUBRIC)

    assert outcome_of(store, job_id) == FilterOutcome.PASS
    assert report.reopened_for_scoring == 1


def titled(store: SqliteStore, now: datetime, title: str, key: str) -> None:
    seed(store, now, prefs(), source_job_id=key, url=f"https://x/{key}", title=title)


def test_the_plan_changes_nothing_in_the_store(store: SqliteStore, now: datetime) -> None:
    job_id = seed(store, now, prefs(), location="Berlin, Germany")

    plan = plan_refilter(store.all_active_jobs(), prefs(allowed_cities=("hamburg",)))

    assert plan.transitions == {"pass→reject": 1}
    assert plan.hidden == 1
    assert outcome_of(store, job_id) == FilterOutcome.PASS


def test_the_plan_says_the_same_as_the_save(store: SqliteStore, now: datetime) -> None:
    seed(store, now, prefs(), location="Berlin, Germany")
    draft = prefs(allowed_cities=("hamburg",))

    plan = plan_refilter(store.all_active_jobs(), draft)
    report = apply_preferences(store, draft, MODEL, RUBRIC)

    assert (plan.examined, len(plan.changes), plan.transitions) == (
        report.examined,
        report.changed,
        report.transitions,
    )


def test_an_excluded_term_matches_any_title_holding_the_word(
    store: SqliteStore, now: datetime
) -> None:
    """Why "manager" is wider than it looks: "Senior Manager" and "Engineering Manager" both hit."""
    titled(store, now, "Senior Manager, Data Engineering", "1")
    titled(store, now, "Engineering Manager", "2")
    titled(store, now, "Data Engineer", "3")

    plan = plan_refilter(store.all_active_jobs(), prefs(excluded_role_terms=("manager",)))

    impact = plan.excluded_role_terms["manager"]
    assert (impact.matches, impact.newly_hidden) == (2, 2)
    assert plan.transitions == {"pass→reject": 2}


def test_a_job_already_rejected_is_a_match_but_not_newly_hidden(
    store: SqliteStore, now: datetime
) -> None:
    titled(store, now, "Head of Manager Relations", "1")  # rejected on seniority already
    titled(store, now, "Data Manager", "2")

    plan = plan_refilter(store.all_active_jobs(), prefs(excluded_role_terms=("manager",)))

    impact = plan.excluded_role_terms["manager"]
    assert (impact.matches, impact.newly_hidden) == (2, 1)


def test_a_term_matches_whole_words_only(store: SqliteStore, now: datetime) -> None:
    titled(store, now, "Management Consultant", "1")

    plan = plan_refilter(store.all_active_jobs(), prefs(excluded_role_terms=("manager",)))

    assert plan.excluded_role_terms["manager"].matches == 0
