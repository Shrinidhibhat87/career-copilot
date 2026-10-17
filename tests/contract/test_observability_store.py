"""The observability store: exact arithmetic on seeded rows, roll-up sums, sessions."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from career_copilot.adapters.store.migrations import MIGRATIONS
from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.ports import LlmCallRecord, Recorder, ToolCallRecord

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def store(tmp_path: Path) -> ObservabilityStore:
    return ObservabilityStore(tmp_path / "jobs.db")


def call(
    out: int | None,
    latency: float,
    outcome: str = "ok",
    model: str = "A",
    stage: str = "match",
    tokens_in: int | None = 100,
) -> LlmCallRecord:
    return LlmCallRecord(
        stage=stage,
        model=model,
        backend="ollama",
        kind="request",
        latency_s=latency,
        outcome=outcome,
        tokens_in=tokens_in if out is not None else None,
        tokens_out=out,
    )


def seed(store: ObservabilityStore) -> None:
    at = NOW - timedelta(days=1)
    for out, latency in ((50, 2.0), (100, 2.0), (150, 2.0)):
        store.record_llm(call(out, latency), at)
    store.record_llm(call(None, 1.0, "transport"), at)


def test_migrations_are_contiguous_from_one() -> None:
    assert [version for version, _ in MIGRATIONS] == list(range(1, len(MIGRATIONS) + 1))


def test_the_store_is_a_recorder(store: ObservabilityStore) -> None:
    assert isinstance(store.recorder(), Recorder)


def test_summary_tiles_are_exact(store: ObservabilityStore) -> None:
    seed(store)
    tiles = store.summary(NOW, days=7)["tiles"]
    assert tiles == {
        "tokensPerS": 42.86,  # 300 output tokens over 7 s
        "avgLatencyS": 1.75,
        "totalTokens": 600,
        "calls": 4,
        "errorRate": 0.25,
    }


def test_summary_rows_per_model_and_stage(store: ObservabilityStore) -> None:
    seed(store)
    store.record_llm(call(10, 1.0, model="B", stage="triage"), NOW - timedelta(days=1))
    rows = {(r["model"], r["stage"]): r for r in store.summary(NOW, days=7)["rows"]}
    a = rows[("A", "match")]
    assert (a["calls"], a["tokensIn"], a["tokensOut"]) == (4, 300, 300)
    assert a["tokensPerSP50"] == 50.0  # median of 25, 50, 75; the failed call has no tokens
    assert (a["latencyP50"], a["latencyP95"]) == (2.0, 2.0)
    assert rows[("B", "triage")]["calls"] == 1


def test_the_window_excludes_older_rows(store: ObservabilityStore) -> None:
    seed(store)
    store.record_llm(call(10, 1.0), NOW - timedelta(days=20))
    assert store.summary(NOW, days=7)["tiles"]["calls"] == 4
    assert store.summary(NOW, days=30)["tiles"]["calls"] == 5


def test_an_empty_store_reads_as_no_calls(store: ObservabilityStore) -> None:
    summary = store.summary(NOW, days=30)
    assert summary["rows"] == []
    assert summary["tiles"]["calls"] == 0
    assert summary["tiles"]["tokensPerS"] == 0.0


def test_tool_rows(store: ObservabilityStore) -> None:
    at = NOW - timedelta(days=1)
    for latency, outcome in ((1.0, "ok"), (3.0, "ok"), (2.0, "refused"), (6.0, "error")):
        store.record_tool(
            ToolCallRecord(agent="discovery", tool="probe_ats", latency_s=latency, outcome=outcome),
            at,
        )
    (row,) = store.tools(NOW, days=7)
    assert (row["agent"], row["tool"], row["calls"]) == ("discovery", "probe_ats", 4)
    assert row["slowestS"] == 6.0
    assert (row["refused"], row["errors"]) == (1, 1)  # a refusal is the tool working
    assert "avgLatencyS" not in row and "p95LatencyS" not in row


def test_series_is_daily_tokens_per_second_per_model(store: ObservabilityStore) -> None:
    seed(store)
    store.record_llm(call(100, 4.0, model="B"), NOW - timedelta(days=2))
    series = store.series(NOW, days=7)
    day1 = (NOW - timedelta(days=1)).date().isoformat()
    day2 = (NOW - timedelta(days=2)).date().isoformat()
    assert {"day": day1, "model": "A", "tokensPerS": 42.86} in series
    assert {"day": day2, "model": "B", "tokensPerS": 25.0} in series


def test_roll_up_folds_old_detail_into_daily_totals_with_equal_sums(
    store: ObservabilityStore,
) -> None:
    old = NOW - timedelta(days=95)
    store.record_llm(call(50, 2.0), old)
    store.record_llm(call(100, 4.0), old)
    store.record_llm(call(None, 1.0, "provider"), old)
    seed(store)  # recent rows must stay as detail
    before = store.summary(NOW, days=120)["tiles"]

    store.roll_up(NOW, detail_days=90)

    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 4
    day = conn.execute(
        "SELECT calls, tokens_in, tokens_out, latency_s, errors FROM llm_daily"
    ).fetchall()
    assert day == [(3, 200, 150, 7.0, 1)]
    after = store.summary(NOW, days=120)["tiles"]
    assert after == before


def test_roll_up_twice_does_not_double_count(store: ObservabilityStore) -> None:
    store.record_llm(call(50, 2.0), NOW - timedelta(days=95))
    store.roll_up(NOW, detail_days=90)
    store.roll_up(NOW, detail_days=90)
    assert store.summary(NOW, days=120)["tiles"]["calls"] == 1


def test_rolled_up_days_report_averages_without_percentiles(store: ObservabilityStore) -> None:
    store.record_llm(call(50, 2.0), NOW - timedelta(days=95))
    store.roll_up(NOW, detail_days=90)
    (row,) = store.summary(NOW, days=120)["rows"]
    assert row["tokensPerSP50"] is None
    assert row["latencyP95"] is None
    assert row["calls"] == 1


def test_tool_roll_up_keeps_sums(store: ObservabilityStore) -> None:
    old = NOW - timedelta(days=100)
    for latency, outcome in ((1.0, "ok"), (3.0, "error")):
        store.record_tool(
            ToolCallRecord(agent="curator", tool="shortlist", latency_s=latency, outcome=outcome),
            old,
        )
    store.roll_up(NOW, detail_days=90)
    (row,) = store.tools(NOW, days=120)
    assert (row["calls"], row["errors"], row["slowestS"]) == (2, 1, None)


def test_stored_columns_hold_only_numbers_names_and_ids(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    store.record_llm(call(50, 2.0), NOW)
    store.record_tool(ToolCallRecord("discovery", "web_search", 1.0, "ok"), NOW)
    conn = sqlite3.connect(store.path)
    for table in ("llm_calls", "tool_calls"):
        info = conn.execute(f"PRAGMA table_info({table})").fetchall()
        for _, name, declared, *_rest in info:
            assert declared in {"INTEGER", "REAL", "TEXT"}, (table, name)
            assert name not in {"prompt", "reply", "content", "text", "args", "arguments"}
        for row in conn.execute(f"SELECT * FROM {table}").fetchall():
            for value in row:
                assert value is None or isinstance(value, (int, float)) or len(str(value)) < 40


def test_a_call_record_has_no_text_field() -> None:
    assert not {"prompt", "reply", "content", "text"} & set(asdict(call(1, 1.0)))


def test_sessions_open_close_and_list(store: ObservabilityStore) -> None:
    first = store.open_session(NOW - timedelta(hours=3), "laptop", "data/logs/a.log")
    store.close_session(first, NOW - timedelta(hours=2), "clean", None)
    second = store.open_session(NOW, "laptop", "data/logs/b.log")
    rows = store.sessions(limit=10)
    assert [r["id"] for r in rows] == [second, first]
    assert rows[1]["outcome"] == "clean"
    assert rows[1]["durationS"] == 3600.0
    assert rows[0]["outcome"] is None
    assert rows[0]["logPath"] == "data/logs/b.log"


def test_unended_sessions_are_listed_for_closing(store: ObservabilityStore) -> None:
    done = store.open_session(NOW - timedelta(hours=3), None, "a.log")
    store.close_session(done, NOW, "clean", None)
    open_one = store.open_session(NOW - timedelta(hours=1), None, "b.log")
    assert [(s_id, path) for s_id, _, path in store.unended_sessions()] == [(open_one, "b.log")]


def test_log_archive_is_stored_gzipped(store: ObservabilityStore) -> None:
    sid = store.open_session(NOW, None, "a.log")
    store.close_session(sid, NOW, "clean", b"\x1f\x8bpayload")
    conn = sqlite3.connect(store.path)
    assert conn.execute("SELECT log_gz FROM app_sessions").fetchone()[0] == b"\x1f\x8bpayload"


def test_today_means_since_local_midnight(store: ObservabilityStore) -> None:
    store.record_llm(call(50, 2.0), NOW - timedelta(minutes=1))
    store.record_llm(call(50, 2.0), NOW - timedelta(hours=30))
    assert store.summary(NOW, days=1)["tiles"]["calls"] == 1
    assert store.summary(NOW, days=7)["tiles"]["calls"] == 2


def test_tool_times_below_a_millisecond_are_not_rounded_away(store: ObservabilityStore) -> None:
    store.record_tool(ToolCallRecord("curator", "get_job", 0.00002, "ok"), NOW)
    (row,) = store.tools(NOW, days=7)
    assert row["slowestS"] == 0.00002


def test_flows_rebuild_each_run_with_exact_step_times(store: ObservabilityStore) -> None:
    start = NOW - timedelta(hours=1)
    store.record_tool(
        ToolCallRecord("curator", "search_pool", 0.5, "ok", agent_run="r1"),
        start + timedelta(seconds=2),
    )
    store.record_tool(
        ToolCallRecord("curator", "shortlist", 0.25, "refused", agent_run="r1"),
        start + timedelta(seconds=3),
    )
    store.record_tool(ToolCallRecord("curator", "get_job", 0.1, "ok"), start)  # no run id: skipped
    store.record_llm(
        LlmCallRecord(
            "curator",
            "m",
            "ollama",
            "agent_run",
            3.25,
            "ok",
            requests=3,
            tokens_in=900,
            tokens_out=60,
            agent_run="r1",
        ),
        start + timedelta(seconds=4),
    )
    (flow,) = store.flows(NOW, days=7)
    assert (flow["agent"], flow["model"], flow["outcome"]) == ("curator", "m", "ok")
    assert (flow["requests"], flow["tokensIn"], flow["tokensOut"]) == (3, 900, 60)
    assert flow["totalS"] == 4.0
    assert flow["steps"] == [
        {"tool": "search_pool", "outcome": "ok", "toolS": 0.5, "stepS": 2.0},
        {"tool": "shortlist", "outcome": "refused", "toolS": 0.25, "stepS": 1.0},
    ]
    assert flow["endS"] == 1.0


def test_a_run_without_tools_is_one_step_to_the_end(store: ObservabilityStore) -> None:
    store.record_llm(
        LlmCallRecord("profiler", "m", "ollama", "agent_run", 5.0, "timeout", agent_run="solo"),
        NOW - timedelta(hours=1),
    )
    (flow,) = store.flows(NOW, days=7)
    assert (flow["steps"], flow["endS"], flow["outcome"]) == ([], 5.0, "timeout")


def test_flows_are_newest_first_and_limited(store: ObservabilityStore) -> None:
    for index in range(3):
        store.record_llm(
            LlmCallRecord("curator", "m", "ollama", "agent_run", 1.0, "ok", agent_run=f"r{index}"),
            NOW - timedelta(hours=3 - index),
        )
    assert [f["totalS"] for f in store.flows(NOW, days=7, limit=2)] == [1.0, 1.0]
    assert len(store.flows(NOW, days=7, limit=2)) == 2
    assert (
        store.flows(NOW, days=7, limit=2)[0]["startedAt"] > store.flows(NOW, days=7)[2]["startedAt"]
    )


def test_sessions_report_whether_a_log_can_be_downloaded(
    store: ObservabilityStore, tmp_path: Path
) -> None:
    real = tmp_path / "a.log"
    real.write_text("hi")
    with_file = store.open_session(NOW, None, str(real))
    gone = store.open_session(NOW, None, str(tmp_path / "gone.log"))
    archived = store.open_session(NOW, None, str(tmp_path / "gone2.log"))
    store.close_session(archived, NOW, "clean", b"\x1f\x8b")
    flags = {r["id"]: r["hasLog"] for r in store.sessions()}
    assert flags == {with_file: True, gone: False, archived: True}


def test_session_log_reads_the_row_only(store: ObservabilityStore) -> None:
    sid = store.open_session(NOW, None, "x.log")
    assert store.session_log(sid) == ("x.log", None)
    assert store.session_log(9999) is None


def priced(eur: float, backend: str = "openrouter", model: str = "R") -> LlmCallRecord:
    return LlmCallRecord(
        stage="match",
        model=model,
        backend=backend,
        kind="request",
        latency_s=1.0,
        outcome="ok",
        tokens_in=10,
        tokens_out=5,
        cost_eur=eur,
    )


def test_cost_sums_by_window_backend_model_and_day(store: ObservabilityStore) -> None:
    store.record_llm(priced(0.25), NOW)
    store.record_llm(priced(0.5), NOW - timedelta(days=3))
    store.record_llm(priced(0.0, backend="ollama", model="L"), NOW)
    store.record_llm(priced(9.0), NOW - timedelta(days=40))

    today = store.cost(NOW, days=1)
    assert today["totalEur"] == 0.25
    week = store.cost(NOW, days=7)
    assert week["totalEur"] == 0.75
    assert {b["backend"]: b["eur"] for b in week["byBackend"]} == {
        "ollama": 0.0,
        "openrouter": 0.75,
    }
    assert [d["eur"] for d in week["byDay"]] == [0.5, 0.25]


def test_cost_survives_roll_up(store: ObservabilityStore) -> None:
    store.record_llm(priced(0.2), NOW - timedelta(days=95))
    store.record_llm(priced(0.3), NOW - timedelta(days=95))
    before = store.cost(NOW, days=120)["totalEur"]
    store.roll_up(NOW, detail_days=90)
    assert store.cost(NOW, days=120)["totalEur"] == before == 0.5
