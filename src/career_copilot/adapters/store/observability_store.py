"""SQLite storage for call records, daily roll-ups and app sessions.

A separate module from `SqliteStore`: every write opens its own short connection, because
recorders are called from worker threads and from agent runs, and a `sqlite3` connection
must not be shared across threads. Rows hold numbers, names and ids only.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from career_copilot.adapters.store.migrations import migrate
from career_copilot.ports import LlmCallRecord, ToolCallRecord

log = logging.getLogger(__name__)

BUSY_TIMEOUT_S = 5.0


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile; None when there is nothing to measure."""
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def window_start(now: datetime, days: int) -> datetime:
    """Where a dashboard window begins. One day means today: since local midnight."""
    if days == 1:
        return now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return now - timedelta(days=days)


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


class ObservabilityStore:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        with self._connect() as conn:
            migrate(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def recorder(self, run_id: int | None = None, profile: str | None = None) -> SqliteRecorder:
        return SqliteRecorder(self, run_id=run_id, profile=profile)

    # --- writes ----------------------------------------------------------

    def record_llm(self, call: LlmCallRecord, now: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO llm_calls (created_at, run_id, stage, model, backend, profile, kind,"
                " requests, tokens_in, tokens_out, latency_s, outcome, retry, agent_run, cost_eur,"
                " cached_tokens)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _iso(now),
                    call.run_id,
                    call.stage,
                    call.model,
                    call.backend,
                    call.profile,
                    call.kind,
                    call.requests,
                    call.tokens_in,
                    call.tokens_out,
                    call.latency_s,
                    call.outcome,
                    call.retry,
                    call.agent_run,
                    call.cost_eur,
                    call.cached_tokens,
                ),
            )

    def record_tool(self, call: ToolCallRecord, now: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO tool_calls"
                " (created_at, run_id, agent, tool, latency_s, outcome, agent_run)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    _iso(now),
                    call.run_id,
                    call.agent,
                    call.tool,
                    call.latency_s,
                    call.outcome,
                    call.agent_run,
                ),
            )

    def roll_up(self, now: datetime, detail_days: int) -> None:
        """Fold detail rows older than `detail_days` into daily totals, then delete them."""
        cutoff = _iso(now - timedelta(days=detail_days))
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO llm_daily (day, model, backend, stage, calls, tokens_in, tokens_out,"
                " latency_s, errors, cost_eur)"
                " SELECT substr(created_at, 1, 10), model, backend, stage, COUNT(*),"
                " COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), SUM(latency_s),"
                " SUM(outcome != 'ok'), SUM(cost_eur) FROM llm_calls WHERE created_at < ?"
                " GROUP BY 1, 2, 3, 4"
                " ON CONFLICT (day, model, backend, stage) DO UPDATE SET"
                " calls = calls + excluded.calls, tokens_in = tokens_in + excluded.tokens_in,"
                " tokens_out = tokens_out + excluded.tokens_out,"
                " latency_s = latency_s + excluded.latency_s, errors = errors + excluded.errors,"
                " cost_eur = cost_eur + excluded.cost_eur",
                (cutoff,),
            )
            conn.execute("DELETE FROM llm_calls WHERE created_at < ?", (cutoff,))
            conn.execute(
                "INSERT INTO tool_daily (day, agent, tool, calls, latency_s, errors)"
                " SELECT substr(created_at, 1, 10), agent, tool, COUNT(*), SUM(latency_s),"
                " SUM(outcome = 'error') FROM tool_calls WHERE created_at < ?"
                " GROUP BY 1, 2, 3"
                " ON CONFLICT (day, agent, tool) DO UPDATE SET"
                " calls = calls + excluded.calls, latency_s = latency_s + excluded.latency_s,"
                " errors = errors + excluded.errors",
                (cutoff,),
            )
            conn.execute("DELETE FROM tool_calls WHERE created_at < ?", (cutoff,))

    # --- reads -----------------------------------------------------------

    def _llm_window(self, now: datetime, days: int) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        since = window_start(now, days)
        with self._connect() as conn:
            detail = conn.execute(
                "SELECT * FROM llm_calls WHERE created_at >= ?", (_iso(since),)
            ).fetchall()
            daily = conn.execute(
                "SELECT * FROM llm_daily WHERE day >= ?", (since.date().isoformat(),)
            ).fetchall()
        return detail, daily

    def summary(self, now: datetime, days: int) -> dict[str, Any]:
        detail, daily = self._llm_window(now, days)
        groups: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
            lambda: {
                "calls": 0,
                "tin": 0,
                "tout": 0,
                "lat": 0.0,
                "errors": 0,
                "speeds": [],
                "lats": [],
            }
        )
        for row in detail:
            g = groups[(row["model"], row["backend"], row["stage"])]
            g["calls"] += 1
            g["tin"] += row["tokens_in"] or 0
            g["tout"] += row["tokens_out"] or 0
            g["lat"] += row["latency_s"]
            g["errors"] += row["outcome"] != "ok"
            g["lats"].append(row["latency_s"])
            if row["tokens_out"] and row["latency_s"] > 0:
                g["speeds"].append(row["tokens_out"] / row["latency_s"])
        for row in daily:
            g = groups[(row["model"], row["backend"], row["stage"])]
            g["calls"] += row["calls"]
            g["tin"] += row["tokens_in"]
            g["tout"] += row["tokens_out"]
            g["lat"] += row["latency_s"]
            g["errors"] += row["errors"]

        calls = sum(g["calls"] for g in groups.values())
        tout = sum(g["tout"] for g in groups.values())
        tin = sum(g["tin"] for g in groups.values())
        lat = sum(g["lat"] for g in groups.values())
        errors = sum(g["errors"] for g in groups.values())
        tiles = {
            "tokensPerS": round(tout / lat, 2) if lat > 0 else 0.0,
            "avgLatencyS": round(lat / calls, 2) if calls else 0.0,
            "totalTokens": tin + tout,
            "calls": calls,
            "errorRate": round(errors / calls, 4) if calls else 0.0,
        }
        rows = [
            {
                "model": model,
                "backend": backend,
                "stage": stage,
                "calls": g["calls"],
                "tokensIn": g["tin"],
                "tokensOut": g["tout"],
                "tokensPerSP50": _r(_percentile(g["speeds"], 0.5)),
                "latencyP50": _r(_percentile(g["lats"], 0.5)),
                "latencyP95": _r(_percentile(g["lats"], 0.95)),
            }
            for (model, backend, stage), g in sorted(groups.items())
        ]
        return {"days": days, "tiles": tiles, "rows": rows}

    def cost(self, now: datetime, days: int) -> dict[str, Any]:
        """What the window cost, in total and by backend, by model and stage, and by day."""
        detail, daily = self._llm_window(now, days)
        by_model: dict[tuple[str, str, str], list[float]] = defaultdict(lambda: [0.0, 0])
        by_day: dict[str, float] = defaultdict(float)
        for row in detail:
            g = by_model[(row["model"], row["backend"], row["stage"])]
            g[0] += row["cost_eur"]
            g[1] += 1
            by_day[row["created_at"][:10]] += row["cost_eur"]
        for row in daily:
            g = by_model[(row["model"], row["backend"], row["stage"])]
            g[0] += row["cost_eur"]
            g[1] += row["calls"]
            by_day[row["day"]] += row["cost_eur"]
        backends: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
        for (_, backend, _), (eur, calls) in by_model.items():
            backends[backend][0] += eur
            backends[backend][1] += calls
        return {
            "days": days,
            "totalEur": round(sum(g[0] for g in by_model.values()), 6),
            "byBackend": [
                {"backend": b, "eur": round(v[0], 6), "calls": int(v[1])}
                for b, v in sorted(backends.items())
            ],
            "byModel": [
                {
                    "model": m,
                    "backend": b,
                    "stage": st,
                    "eur": round(v[0], 6),
                    "calls": int(v[1]),
                }
                for (m, b, st), v in sorted(by_model.items())
            ],
            "byDay": [{"day": d, "eur": round(e, 6)} for d, e in sorted(by_day.items())],
        }

    def tools(self, now: datetime, days: int) -> list[dict[str, Any]]:
        since = window_start(now, days)
        with self._connect() as conn:
            detail = conn.execute(
                "SELECT * FROM tool_calls WHERE created_at >= ?", (_iso(since),)
            ).fetchall()
            daily = conn.execute(
                "SELECT * FROM tool_daily WHERE day >= ?", (since.date().isoformat(),)
            ).fetchall()
        groups: dict[tuple[str, str], dict[str, Any]] = defaultdict(
            lambda: {"calls": 0, "refused": 0, "errors": 0, "slowest": None}
        )
        for row in detail:
            g = groups[(row["agent"], row["tool"])]
            g["calls"] += 1
            g["refused"] += row["outcome"] == "refused"
            g["errors"] += row["outcome"] == "error"
            g["slowest"] = max(g["slowest"] or 0.0, row["latency_s"])
        for row in daily:
            g = groups[(row["agent"], row["tool"])]
            g["calls"] += row["calls"]
            g["errors"] += row["errors"]
        return [
            {
                "agent": agent,
                "tool": tool,
                "calls": g["calls"],
                # Local tools finish in microseconds; rounding to centiseconds read as 0.
                "slowestS": None if g["slowest"] is None else round(g["slowest"], 6),
                "refused": g["refused"],
                "errors": g["errors"],
            }
            for (agent, tool), g in sorted(groups.items())
        ]

    def flows(self, now: datetime, days: int, limit: int = 5) -> list[dict[str, Any]]:
        """The latest agent runs, each as its tool calls in order with the time of each step.

        A step is the time from the previous step finishing to this tool call finishing: the
        model deciding plus the tool running. The tool's own time is far smaller.
        """
        since = _iso(window_start(now, days))
        with self._connect() as conn:
            runs = conn.execute(
                "SELECT * FROM llm_calls WHERE kind = 'agent_run' AND agent_run IS NOT NULL"
                " AND created_at >= ? ORDER BY id DESC LIMIT ?",
                (since, limit),
            ).fetchall()
            tool_rows = (
                conn.execute(
                    "SELECT * FROM tool_calls WHERE agent_run IN ("
                    + ",".join("?" * len(runs))
                    + ") ORDER BY id",
                    [r["agent_run"] for r in runs],
                ).fetchall()
                if runs
                else []
            )
        by_run: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for tool_row in tool_rows:
            by_run[tool_row["agent_run"]].append(tool_row)
        flows: list[dict[str, Any]] = []
        for run in runs:
            tools = by_run[run["agent_run"]]
            ended = datetime.fromisoformat(run["created_at"])
            total = run["latency_s"] + sum(t["latency_s"] for t in tools)
            started = ended - timedelta(seconds=total)
            cursor = started
            steps = []
            for t in tools:
                at = datetime.fromisoformat(t["created_at"])
                steps.append(
                    {
                        "tool": t["tool"],
                        "outcome": t["outcome"],
                        "toolS": round(t["latency_s"], 6),
                        "stepS": round(max((at - cursor).total_seconds(), 0.0), 2),
                    }
                )
                cursor = at
            flows.append(
                {
                    "agent": run["stage"],
                    "model": run["model"],
                    "outcome": run["outcome"],
                    "requests": run["requests"],
                    "tokensIn": run["tokens_in"],
                    "tokensOut": run["tokens_out"],
                    "startedAt": started.isoformat(),
                    "totalS": round(total, 2),
                    "steps": steps,
                    "endS": round(max((ended - cursor).total_seconds(), 0.0), 2),
                }
            )
        return flows

    def series(self, now: datetime, days: int) -> list[dict[str, Any]]:
        detail, daily = self._llm_window(now, days)
        totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
        for row in detail:
            t = totals[(row["created_at"][:10], row["model"])]
            t[0] += row["tokens_out"] or 0
            t[1] += row["latency_s"]
        for row in daily:
            t = totals[(row["day"], row["model"])]
            t[0] += row["tokens_out"]
            t[1] += row["latency_s"]
        return [
            {"day": day, "model": model, "tokensPerS": round(out / lat, 2)}
            for (day, model), (out, lat) in sorted(totals.items())
            if out > 0 and lat > 0
        ]

    # --- sessions --------------------------------------------------------

    def open_session(self, started: datetime, profile: str | None, log_path: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO app_sessions (started_at, profile, log_path) VALUES (?,?,?)",
                (_iso(started), profile, log_path),
            )
            return int(cur.lastrowid or 0)

    def close_session(
        self, session_id: int, ended: datetime, outcome: str, log_gz: bytes | None
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE app_sessions SET ended_at = ?, outcome = ?, log_gz = COALESCE(?, log_gz)"
                " WHERE id = ?",
                (_iso(ended), outcome, log_gz, session_id),
            )

    def unended_sessions(self) -> list[tuple[int, datetime, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, log_path FROM app_sessions WHERE ended_at IS NULL"
            ).fetchall()
        return [(r["id"], datetime.fromisoformat(r["started_at"]), r["log_path"]) for r in rows]

    def session_log(self, session_id: int) -> tuple[str, bytes | None] | None:
        """Where a session's log lives and its archived copy, read from the row alone."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT log_path, log_gz FROM app_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        return None if row is None else (row["log_path"], row["log_gz"])

    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, ended_at, outcome, profile, log_path,"
                " log_gz IS NOT NULL AS archived FROM app_sessions"
                " ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            duration = None
            if r["ended_at"]:
                span = datetime.fromisoformat(r["ended_at"]) - datetime.fromisoformat(
                    r["started_at"]
                )
                duration = span.total_seconds()
            out.append(
                {
                    "id": r["id"],
                    "startedAt": r["started_at"],
                    "endedAt": r["ended_at"],
                    "durationS": duration,
                    "outcome": r["outcome"],
                    "profile": r["profile"],
                    "logPath": r["log_path"],
                    "hasLog": bool(r["archived"]) or Path(r["log_path"]).is_file(),
                }
            )
        return out


class SqliteRecorder:
    """The `Recorder` over `ObservabilityStore`. A failed write is logged and dropped."""

    def __init__(
        self,
        store: ObservabilityStore,
        run_id: int | None = None,
        profile: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._profile = profile
        self._clock = clock

    def llm_call(self, call: LlmCallRecord) -> None:
        try:
            self._store.record_llm(call, self._clock())
        except Exception as exc:
            log.warning("recording a model call failed: %s", type(exc).__name__)

    def tool_call(self, call: ToolCallRecord) -> None:
        try:
            self._store.record_tool(call, self._clock())
        except Exception as exc:
            log.warning("recording a tool call failed: %s", type(exc).__name__)
