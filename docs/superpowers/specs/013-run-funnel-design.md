# Run Funnel — Design

> Records how long each stage of a run took and how many jobs survived each step, and shows both on the Runs page.
> Core rule: a run answers "how many sources, how long, and why only N" from stored data, not from guessing.
> Builds on spec 006 (runs and observability) and spec 002 (scoring and digest).

## Decisions

| Topic | Decision |
|---|---|
| Stage times | `PipelineRun.execute` records a start and end for `discover`, `profile`, `score` and `digest`. |
| Funnel | The run stores one JSON funnel: what discovery did, then what scoring did, with the reason the run stopped. |
| Sources | The funnel lists every target as searched, skipped (with the reason: cooldown, target met, no adapter) or failed. |
| Why it stopped | One phrase: `target reached`, `out of candidates`, `match-call cap` or `budget`. A cancelled or failed run has no funnel. |
| Agent calls | The profiler and curator calls carry the run id, so a run's model calls can be grouped. |
| Log | Each stage writes one INFO line to the app log: stage, seconds and its main counts. |
| Page | A "How long it took" card appears only for a run the user opened or just started, never by default. It leads with the total time; the run number is small print. Stage times are one horizontal bar sized by seconds. The funnel is three donuts (sources, jobs, scoring) with a bulleted legend; a click on a slice or legend row opens its explanation and any names. Old runs without details show nothing. |
| Rejections | Each rejected job is counted once under its first reason ("location outside Germany"). The top eight reasons are stored with their counts, so a high rejection share can be explained. |
| Not stored | Which job had which reason, per-source timings and model cost per call. |

## Shapes

```sql
-- migration 23
CREATE TABLE run_stages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES runs (id),
    stage      TEXT    NOT NULL,   -- discover | profile | score | digest
    started_at TEXT    NOT NULL,
    ended_at   TEXT                -- NULL while running, or if the run died inside it
);
CREATE INDEX idx_run_stages_run ON run_stages (run_id, id);
ALTER TABLE runs ADD COLUMN funnel TEXT;   -- JSON below; NULL for older runs
```

```python
funnel = {
    "discover": {
        "targets": 8, "searched": 6, "fetched": 345, "inserted": 40,
        "rejected": 290, "review": 15, "qualified": 28,
        "rejectReasons": [{"reason": "location outside Germany", "count": 250}],
        "boards": [{"target": "greenhouse:helsing", "fetched": 100}],
        "skipped": [{"target": "greenhouse:n26", "reason": "cooldown"}],
        "failed": [{"target": "greenhouse:x", "error": "..."}],
    },
    "score": {
        "candidates": 39, "capped": 19, "matched": 19, "above": 18,
        "target": 50, "minScore": 30, "failed": 1, "stoppedBy": "out of candidates",
    },
}
```

## Flow

```
execute():
  for stage in discover, profile, score, digest:
      start_stage(run, stage); log "stage start"
      run it
      end_stage(run, stage); log "stage done in Ns: counts"
  funnel = build_funnel(discover_report, score_report, min_score)
  record_funnel(run, funnel)
  finish_run(...)                       # the key=value summary stays as it is
on failure inside a stage:
  the stage keeps ended_at NULL; the run is finalized as failed as before
stopped_by(score):
  aborted for budget -> "budget"
  above >= target -> "target reached"
  matched >= candidates - capped -> "out of candidates"
  otherwise -> "match-call cap"
GET /api/runs: each run adds stages[{stage, startedAt, endedAt, seconds}] and funnel (or null)
```

## Testing

| Layer | What it proves |
|---|---|
| Unit | `build_funnel` counts searched, skipped and failed targets and picks the right `stoppedBy` for each case. |
| Contract | `start_stage`/`end_stage`/`record_funnel` round-trip through `get_run`; an unfinished stage reads `endedAt: null`; migration 23 keeps old rows. |
| E2E | A pipeline run on fixtures writes four stages in order and a funnel whose counts match the score report; a failing stage leaves it open. |
| E2E | The profiler and curator `llm_calls` rows carry the run id. |
| Frontend | The run card shows the stage table and funnel, and shows nothing for a run without them. |
| Manual | A real run: the page's stage seconds add up to about the run's duration, and the funnel explains the final count. |

## Non-goals

Per-job rejection reasons, per-source timings, cost per model call, a currency fix, changes to what a run does.
