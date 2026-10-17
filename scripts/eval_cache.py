"""A/B evaluation of provider prompt-prefix caching on the match stage.

Never touches the live account: `prepare` copies a database (read-only) into an eval
folder and freezes the pool to N jobs; `run` scores that same pool three times, each on a
fresh copy of the frozen database, through the real score pipeline.

    A   nonce prepended to the system prompt per call, so no prefix can ever hit  (baseline)
    B1  normal prompt, first pass: writes the provider cache
    B2  normal prompt, second pass: should read it

    uv run python scripts/eval_cache.py prepare --source <main>/data/users/NAME/jobs.db
    uv run python scripts/eval_cache.py run --config <main>/config.yaml --env <main>/.env
    uv run python scripts/eval_cache.py report
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import statistics
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
DEFAULT_OUT = HERE / "eval-cache"
ARMS = ("A-no-cache", "B1-cold", "B2-warm")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# --- prepare ------------------------------------------------------------------------


def prepare(source: Path, out: Path, jobs: int) -> None:
    if not source.exists():
        sys.exit(f"source database not found: {source}")
    out.mkdir(parents=True, exist_ok=True)
    base = out / "base.db"
    if base.exists():
        base.unlink()
    # Read-only open + the sqlite backup API: a consistent copy, the source never written.
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(base)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()

    conn = _connect(base)
    with conn:
        picked = [
            row["job_id"]
            for row in conn.execute(
                "SELECT job_id FROM scores WHERE stage = 'match' "
                "GROUP BY job_id ORDER BY MAX(id) DESC LIMIT ?",
                (jobs,),
            )
        ]
        if len(picked) < jobs:
            print(f"warning: only {len(picked)} previously matched jobs available")
        marks = ",".join("?" * len(picked))
        # Everything outside the pool is dismissed for good, so scoring cannot reach it.
        conn.execute(
            f"INSERT INTO job_decisions (job_id, decision, note, created_at) "
            f"SELECT id, 'dismissed', 'eval-cache', ? FROM jobs WHERE id NOT IN ({marks})",
            (datetime.now(UTC).isoformat(), *picked),
        )
        # The pool needs scoring again: drop its scores, and any dismissal/save on it.
        conn.execute(f"DELETE FROM scores WHERE job_id IN ({marks})", picked)
        conn.execute(
            f"DELETE FROM job_decisions WHERE job_id IN ({marks})",
            picked,
        )
        # A run left 'running' in the copy would block begin_run.
        conn.execute(
            "UPDATE runs SET state = 'failed', ended_at = COALESCE(ended_at, started_at) "
            "WHERE state IN ('running', 'syncing') OR ended_at IS NULL"
        )
    (out / "pool.json").write_text(json.dumps({"job_ids": picked, "source": str(source)}))
    print(f"prepared {base} with a pool of {len(picked)} jobs")
    print("next: uv run python scripts/eval_cache.py run --config <config.yaml> --env <.env>")


# --- run ----------------------------------------------------------------------------


class NonceClient:
    """Wraps a real client so every call has a unique system prefix. Baseline arm only."""

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def complete(self, prompt, schema, system=None, validator=None):  # type: ignore[no-untyped-def]
        marked = f"[run {uuid.uuid4().hex}] {system or ''}"
        return self._inner.complete(prompt, schema, system=marked, validator=validator)  # type: ignore[attr-defined]


def _score_once(config_path: str, work_db: Path, arm: str) -> int:
    import os

    from career_copilot.adapters.store.sqlite_store import SqliteStore, process_started_at
    from career_copilot.config import Config
    from career_copilot.usecases import pipeline
    from career_copilot.usecases.model_settings import in_force

    config = Config.load(config_path)
    config = config.model_copy(update={"db_path": str(work_db)})
    config = in_force(config)

    if arm == "A-no-cache":
        real = pipeline.build_client

        def wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
            return NonceClient(real(*args, **kwargs))

        pipeline.build_client = wrapped  # type: ignore[assignment]
    store = SqliteStore(str(work_db))
    try:
        now = datetime.now(UTC)
        frozen = pipeline.freeze_config(
            config, store, pipeline.current_preferences(store, config), initial=False
        )
        run_id = store.begin_run("score", frozen, now, os.getpid(), process_started_at(os.getpid()))
        try:
            profile = pipeline.refresh_profile(config, store, store.run_config(run_id))
            report = pipeline.build_score_run(config, store, run_id, profile).execute()
            store.finish_run(run_id, datetime.now(UTC), f"matched={report.matched}")
        except BaseException as exc:
            pipeline.finalize_error(store, run_id, exc)
            raise
        print(
            f"  {arm}: run {run_id} matched {report.matched} "
            f"triaged {report.triaged} cost €{report.cost_eur:.4f}"
        )
        return run_id
    finally:
        store.close()


def run(config_path: str, env_path: str | None, out: Path, cap_eur: float) -> None:
    from dotenv import load_dotenv

    base = out / "base.db"
    if not base.exists():
        sys.exit("run `prepare` first")
    if env_path:
        load_dotenv(env_path)
    spent = 0.0
    results: dict[str, int] = {}
    for arm in ARMS:
        if spent >= cap_eur:
            print(f"stopping before {arm}: cap €{cap_eur:.2f} reached (€{spent:.4f})")
            break
        work = out / f"{arm}.db"
        shutil.copyfile(base, work)
        print(f"arm {arm}")
        results[arm] = _score_once(config_path, work, arm)
        with _connect(work) as conn:
            spent += conn.execute(
                "SELECT COALESCE(SUM(cost_eur),0) FROM llm_calls WHERE run_id = ?",
                (results[arm],),
            ).fetchone()[0]
    (out / "runs.json").write_text(json.dumps(results))
    print(f"spent €{spent:.4f}")
    report(out)


# --- report -------------------------------------------------------------------------


def _arm_stats(path: Path, run_id: int) -> dict[str, float]:
    conn = _connect(path)
    rows = conn.execute(
        "SELECT stage, tokens_in, tokens_out, cached_tokens, latency_s, cost_eur, outcome "
        "FROM llm_calls WHERE run_id = ? AND kind = 'request'",
        (run_id,),
    ).fetchall()
    match = [r for r in rows if r["stage"] == "match"]
    tokens_in = sum(r["tokens_in"] or 0 for r in match)
    cached = sum(r["cached_tokens"] or 0 for r in match)
    latencies = [r["latency_s"] for r in match]
    # Wall time proxy: first call start to last call end (the in-process run has no stage rows).
    times = conn.execute(
        "SELECT MIN(created_at) AS first, MAX(created_at) AS last FROM llm_calls "
        "WHERE run_id = ? AND stage = 'match'",
        (run_id,),
    ).fetchone()
    wall = 0.0
    if times and times["first"]:
        wall = (
            datetime.fromisoformat(times["last"]) - datetime.fromisoformat(times["first"])
        ).total_seconds()
    return {
        "match_calls": len(match),
        "retries": sum(1 for r in match if r["outcome"] != "ok"),
        "tokens_in": tokens_in,
        "cached_tokens": cached,
        "hit_rate": cached / tokens_in if tokens_in else 0.0,
        "cost_eur": sum(r["cost_eur"] for r in match),
        "cost_per_call_eur": (sum(r["cost_eur"] for r in match) / len(match)) if match else 0.0,
        "latency_mean_s": statistics.fmean(latencies) if latencies else 0.0,
        "latency_p50_s": statistics.median(latencies) if latencies else 0.0,
        "match_span_s": wall,
        "other_cost_eur": sum(r["cost_eur"] for r in rows if r["stage"] != "match"),
    }


def report(out: Path) -> None:
    runs = json.loads((out / "runs.json").read_text())
    stats = {arm: _arm_stats(out / f"{arm}.db", run_id) for arm, run_id in runs.items()}
    cols = list(next(iter(stats.values())))
    width = max(len(a) for a in stats) + 2
    print("\n" + "metric".ljust(18) + "".join(a.rjust(width) for a in stats))
    for col in cols:
        cells = "".join(
            (
                f"{stats[a][col]:.4f}" if "eur" in col or "rate" in col else f"{stats[a][col]:.1f}"
            ).rjust(width)
            for a in stats
        )
        print(col.ljust(18) + cells)
    if "A-no-cache" in stats and "B2-warm" in stats:
        a, b = stats["A-no-cache"], stats["B2-warm"]
        if a["cost_eur"]:
            print(f"\nB2 vs A: cost {100 * (b['cost_eur'] / a['cost_eur'] - 1):+.1f}%", end="")
        if a["latency_mean_s"]:
            print(f", latency {100 * (b['latency_mean_s'] / a['latency_mean_s'] - 1):+.1f}%")
    (out / "report.json").write_text(json.dumps(stats, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--jobs", type=int, default=12)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    r = sub.add_parser("run")
    r.add_argument("--config", required=True)
    r.add_argument("--env", default=None)
    r.add_argument("--cap-eur", type=float, default=0.50)
    r.add_argument("--out", type=Path, default=DEFAULT_OUT)
    q = sub.add_parser("report")
    q.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    if args.cmd == "prepare":
        prepare(args.source, args.out, args.jobs)
    elif args.cmd == "run":
        run(args.config, args.env, args.out, args.cap_eur)
    else:
        report(args.out)


if __name__ == "__main__":
    main()
