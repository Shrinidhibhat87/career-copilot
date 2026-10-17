from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.match import MatchAgent
from career_copilot.agents.triage import TriageAgent
from career_copilot.domain.scoring import RUBRIC_VERSION
from career_copilot.ports import LLMResponse, ResponseValidator, SchemaError
from career_copilot.usecases.score import ScoreBudget, ScoreRun
from tests.e2e.test_score_and_digest import MATCH_REPLY, NOW, PROFILE, seed


@pytest.fixture
def store() -> Iterator[SqliteStore]:
    with SqliteStore(":memory:") as s:
        yield s


class SlowLLM:
    """Match client that sleeps, records which threads served it, and can fail a job."""

    model = "slow"

    def __init__(self, delay: float = 0.1, fail_on: str | None = None) -> None:
        self.delay = delay
        self.fail_on = fail_on
        self.threads: set[int] = set()
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse:
        with self._lock:
            self.threads.add(threading.get_ident())
            self.prompts.append(prompt)
        time.sleep(self.delay)
        if self.fail_on and self.fail_on in prompt:
            raise SchemaError("not valid JSON", calls=2, cost_eur=0.01)
        data = FakeLLM(default=MATCH_REPLY).complete(prompt, schema, system, validator).data
        return LLMResponse(data, self.model, 0.01, calls=1)


def build(
    store: SqliteStore,
    match: SlowLLM,
    *,
    target: int,
    concurrency: int,
    max_eur: float = 0.0,
    match_reserve_eur: float = 0.0,
) -> ScoreRun:
    return ScoreRun(
        store=store,
        triage=TriageAgent(FakeLLM(default={"relevance": 60, "reason": "fits"}), ""),
        match=MatchAgent(match, PROFILE),
        clock=lambda: NOW,
        budget=ScoreBudget(
            target=target,
            max_eur=max_eur,
            mode="test",
            match_reserve_eur=match_reserve_eur,
        ),
        rubric_version=RUBRIC_VERSION,
        max_per_company=100,
        match_concurrency=concurrency,
    )


def test_parallel_match_is_faster_and_uses_several_threads(store: SqliteStore) -> None:
    seed(store, 8)
    match = SlowLLM(delay=0.2)
    started = time.monotonic()
    report = build(store, match, target=8, concurrency=4).execute()
    elapsed = time.monotonic() - started
    assert report.matched == 8
    assert len(match.threads) > 1
    assert elapsed < 8 * 0.2 * 0.6  # serial would be 1.6s


def test_concurrency_one_stays_on_one_worker_and_matches_serial_result(
    store: SqliteStore,
) -> None:
    seed(store, 5)
    match = SlowLLM(delay=0.0)
    report = build(store, match, target=5, concurrency=1).execute()
    assert report.matched == 5
    assert len(match.threads) == 1


def test_calls_in_flight_at_the_target_are_kept(store: SqliteStore) -> None:
    seed(store, 10)
    match = SlowLLM(delay=0.05)
    report = build(store, match, target=2, concurrency=4).execute()
    # Never more than one window past the target, and nothing paid for is dropped.
    assert 2 <= report.matched <= 2 + 3
    assert report.matched == len(match.prompts)
    assert report.above_threshold >= 2


def test_one_bad_reply_does_not_stop_the_others(store: SqliteStore) -> None:
    seed(store, 6)
    match = SlowLLM(delay=0.02, fail_on="Forward Deployed Engineer 2")
    report = build(store, match, target=6, concurrency=3).execute()
    assert report.matched == 5
    assert report.failed == 1
    # 6 triage + 5 good matches + the bad one's 2 attempts: nothing billed goes unmetered.
    assert report.llm_calls == 13


def test_reserve_counts_calls_in_flight(store: SqliteStore) -> None:
    seed(store, 10)
    match = SlowLLM(delay=0.05)
    # Ceiling 0.5, reserve 0.2 per call: a third concurrent call would not fit.
    report = build(
        store, match, target=10, concurrency=4, max_eur=0.5, match_reserve_eur=0.2
    ).execute()
    assert report.aborted
    assert len(match.prompts) <= 2 + 1
