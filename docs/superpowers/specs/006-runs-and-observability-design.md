# Runs and Observability — Design

> The whole pipeline (discover, score, digest) runs from the browser or the CLI as one tracked run. Every model and agent-tool call is recorded with tokens and timing. Settings shows both. Core rule: a run spends only what was approved, always ends in a terminal state, and recording never breaks a run or stores prompt or reply text. Builds on 002-scoring-and-digest (`CostMeter`, `ScoreRun`), 005-preferences (frozen into each run), 004-pii-gateway (error text rules) and `LLMResponse` in `ports.py`.

## Decisions

| Topic | Decision |
|---|---|
| One entry point | `usecases/pipeline.py` builds every stage. The CLI (`__main__.py`) and the API (`adapters/api/runs.py`) both call it. |
| Who creates the row | The starter calls `SqliteStore.begin_run`. `PipelineRun` and each stage get a `run_id` and never create or finish the row on their own. |
| Where it runs | One worker thread (`ThreadPoolExecutor(max_workers=1)`) with its own `SqliteStore`. The web server's event loop never runs pipeline code. |
| Progress | SQLite is the truth. The browser polls `GET /api/runs/{id}`. Each per-job update also writes `heartbeat_at`. |
| One run at a time | A partial unique index on `runs (state) WHERE state = 'running'`. A second insert fails and the API answers 409. A company sync is `syncing`, so it never blocks a scoring run. |
| Frozen config | `begin_run` copies the run's `RunConfig` onto the row (`config_json` plus the model, rubric, preferences and budget columns). A preferences save mid-run changes nothing in that run. |
| Cost gate | Confirming approves a spending ceiling and a configuration, never a list of jobs. Discovery may add candidates. `CostMeter.reserve` still holds the ceiling. |
| Preview | `preview_score` reads only. It builds no client and writes no row. `score --dry-run` prints it. |
| Stale preview | The token is a hash of the whole `RunConfig` (preferences, models, rubric, mode, budget, reserves, target). A mismatch, or a different ceiling, is 409 "preview is stale". New candidates do not change it. |
| Failure types | `FailureCategory` on `LLMCallError`: transport, credit, rate_limit, provider, schema, evidence. |
| Retry | Only `schema` retries, once, with the error fed back. Every other category costs one call. |
| Empty reply | An error finish reason, a content filter stop or empty content is `provider`, checked before JSON parsing. |
| Timeouts | `REQUEST_TIMEOUT = 120` seconds on every provider call. `HEARTBEAT_TIMEOUT = 300`. |
| Dead runs | A `running` row is failed as `interrupted` only if its heartbeat is older than 300 s and its owner process is gone (pid missing, or start time differs). Checked in `begin_run` and at API startup, never on a read. |
| Stalled | `GET /api/runs/{id}` reports `stalled: true` from the heartbeat for display. It writes nothing. |
| Shutdown | The lifespan hook sets a cancel event and waits 10 s. The worker stops between jobs and ends its own row `aborted / shutdown`. The hook never finalizes a worker that may still spend. |
| Auto refresh | `POST /api/runs/refresh` starts a run without a preview only when no role is billed, a CV exists and a backend is ready. With `ifStale` it skips when the last good run is under 20 h old. If a run is active it queues one more. |
| Model calls | SQLite only, behind the `Recorder` port. `LiteLLMClient` times each request and reads token usage. `LLMResponse` carries `tokens_in`, `tokens_out`, `latency_s`. Failures and each schema attempt are recorded too. |
| Agent runs | Discovery, curator, profiler, condenser and assistant each write one `agent_run` row. `timed_tool` records each tool call as `ok`, `refused` or `error`. Agent time is run time minus tool time. |
| No text | Prompts, replies and tool arguments are never stored. A recorder error is logged and dropped. |
| Log file | Each app start writes `data/logs/app-<start>.log`. First line "session start", last line "session end". |
| Unclean end | At start, a session with no end is closed as `unclean` at its log's last write time. |
| Log keeping | `observability.log_days` (14) deletes old logs. `observability.archive_logs` (false) gzips the log into the session row. |
| Data keeping | `observability.detail_days` (90) folds older per-call rows into daily totals. Older days report averages only. |
| Settings page | One centred column, title "Settings". Order: spending approval (only if a role is billed), Observability (open), Results (closed), Run history (closed). |
| Billed or local | `GET /api/health/models` gives `local` per role. The approval card shows when any role has `local: false`. If the health call fails, the page treats roles as local and the server still demands the token. |
| Dashboard | Window Today, 7, 30 or 90 days (default 7). Tiles: tokens per second, average latency, total tokens, calls, error rate. Table per model and stage. Tool totals in a closed group. Daily tokens-per-second line per model as inline SVG. Agent run flows for Today only. Sessions list with log download. |
| Run history | Row reads "Run 12 · full · succeeded" plus time, no cost. A legacy row with kind `discover` shows "—". Succeeded rows are tinted green, failed and aborted red, live ones neutral. The state is also a word. |
| Results | Read-only table with a Cost column, filter by outcome, server-side sort and paging. Rule outcomes look different from model scores. |

## Shapes

```python
# domain/runs.py
@dataclass(frozen=True, slots=True)
class RunConfig:      # frozen at start; token() hashes payload()
    preferences: Preferences; triage_model: str; match_model: str; rubric_version: int
    mode: str; budget_eur: float; target: int
    triage_reserve_eur: float; match_reserve_eur: float
    triage_max_output_tokens: int; match_max_output_tokens: int

@dataclass(frozen=True, slots=True)
class ScorePreview:   # candidates_now is a pre-discovery estimate
    candidates_now: int; estimated_calls: int; reserved_eur: float
    ceiling_eur: float; mode: str; token: str
```

```sql
-- migration 4: the runs ledger (kinds: discover, score, digest, full, company_sync)
ALTER TABLE runs ADD COLUMN state TEXT NOT NULL DEFAULT 'succeeded';  -- running|syncing|succeeded|aborted|failed
ALTER TABLE runs ADD COLUMN failure_category TEXT;  -- FailureCategory or interrupted|internal|shutdown|budget|resume_missing
ALTER TABLE runs ADD COLUMN owner_pid INTEGER;      -- with owner_started_at, tells a live owner from a dead one
ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
ALTER TABLE runs ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';
CREATE UNIQUE INDEX idx_runs_single_active ON runs (state) WHERE state = 'running';
-- also kind, stage, completed, total, failed, budget_eur, mode, preferences_version/hash,
-- triage_model, match_model, rubric_version, failure_message, owner_started_at
-- migration 16: llm_calls, tool_calls (detail), llm_daily, tool_daily (roll-ups), app_sessions
```

```python
# ports.py
class Recorder(Protocol):
    def llm_call(self, call: LlmCallRecord) -> None: ...
    def tool_call(self, call: ToolCallRecord) -> None: ...
```

```
POST /api/runs/preview   -> {candidatesNow, estimatedCalls, reservedEur, ceilingEur, mode, token, modelBackendReady, modelBackendProblem}
POST /api/runs           {token, ceilingEur} -> {runId, state}   422 bad body, 409 stale | active | backend not ready, 500 worker not started
POST /api/runs/refresh   {ifStale?} -> {started, runId?, reason?}
GET  /api/runs[/{id}]    -> {state, kind, stage, completed, total, costEur, budgetEur, stalled, failureCategory, failureMessage, sourceFailures, ...}
GET  /api/jobs           outcome, sort (score|triage|title|company|age|cost), direction, page, limit
GET  /api/observability/{summary,tools,flows,series}?days=1|7|30|90, /sessions, /sessions/{id}/log
```

```yaml
observability: {log_days: 14, detail_days: 90, archive_logs: false}   # config.yaml, all optional
```

## Flow

```
start (POST /api/runs):
    422 unless token and finite ceiling > 0; 409 unless diagnose(config).ok
    in one transaction:
        frozen = freeze_config(config, store, preferences)
        409 "preview is stale" if token != frozen.token() or ceiling != frozen.budget_eur
        begin_run: reclaim a dead run, INSERT 'running' with frozen config
                   (unique index hit -> RunInProgress -> 409 "a run is already active")
    submit worker; if submit raises: finalize the row failed/internal, answer 500

worker: store = own SqliteStore; PipelineRun(store, config, run_id, cancel).execute()
    discover -> mark old jobs inactive -> refresh profile -> score -> digest -> finish_run
    cancel checked between jobs; progress + heartbeat per job
    exception -> finalize_error:
        RunCancelled | KeyboardInterrupt -> aborted/shutdown       BudgetExceeded -> aborted/budget
        ResumeMissingError -> failed/resume_missing                LLMCallError -> failed/<category>
        anything else -> failed/internal (fixed sentence, no provider text)
    finally: a conditional update fails any row still 'running'; close store

provider call (LiteLLMClient._call):
    exception -> 402 credit, 429 rate_limit, else transport; message never copies provider text
    reply: error finish reason | content filter | empty -> provider (before json.loads)
    record(tokens, latency, outcome) in every branch; try/except around record

reclaim (begin_run, API startup):
    row running AND heartbeat older than 300 s AND owner pid is not this process
        AND (pid gone OR start time differs) -> failed/interrupted
    PermissionError on the pid check -> leave it running
    a syncing row is reclaimed the same way, measured from started_at until its first heartbeat

app start: close unclean sessions; roll up rows older than detail_days; delete old logs; open log
app end:   write "session end"; gzip if archive_logs; session outcome = clean
dashboard: last detail_days from detail rows, older days from daily rows (averages, no percentiles)
settings:  roles = health; show approval card only if some role is not local; sections follow
```

## Testing

CI runs `ruff`, `mypy` and `uv run pytest`. Vitest runs with `npm test` in `src/frontend`. The model is always `FakeLLM` or a stubbed `completion`.

| Layer | What it proves |
|---|---|
| `tests/contract/test_runs_store.py` | Config round-trips as a snapshot. Two connections get one slot. Fresh heartbeats and live owners survive. Only a stale run with a dead owner is reclaimed. Progress and terminal updates are conditional. A sync never blocks a score run. |
| `tests/contract/test_runs_api.py` | Preview writes nothing. A stale preview, a missing backend and a paid role without a token are refused. Start freezes config. A failed submit finalizes the row. Startup reclaims a dead run. A worker past the grace period is left alone. `/api/jobs` filters, sorts, pages. |
| `tests/unit/test_run_preview.py` | New candidates keep the token valid. A preferences or model change breaks it. |
| `tests/e2e/test_pipeline_run.py` | Progress fires once per paid job. Every failure path ends terminal. A cancelled run is finalized by its worker. |
| `tests/contract/test_litellm_client.py` | One test per category asserts category and call count (one, two for `schema`). Empty content is `provider`. Timeout reaches the provider. Provider text is not copied into errors. |
| `tests/contract/test_refresh_api.py` | A billed role, no CV, a fresh run and a ready local backend each give the stated answer. A save during a run queues one more. |
| `tests/contract/test_health_api.py` | `local` is true for ollama and vllm, false for openrouter. |
| `tests/unit/test_llm_recording.py`, `test_agent_recording.py` | 100 tokens in, 50 out, 2 s records 25 tokens/s. A failure records its category. An agent run gives one `agent_run` row and one row per tool call, including a refusal. A raising recorder does not fail the call. No field holds text. |
| `tests/contract/test_observability_store.py`, `test_observability_api.py` | Tiles and rows are exact for seeded data. Roll-up sums equal the detail sums and a second roll-up adds nothing. Logs outside `data/logs` are never served. |
| `tests/unit/test_app_session.py` | First and last log lines, unclean close, log cleanup, archive off by default, a failing start never raises. |
| vitest `settings`, `settings-css`, `observability` | No approval card when all roles are local. Card shows for a billed role. Sections open and closed as stated. History rows have no euro sign and carry state. Tints map to the tokens. Empty state reads "No calls recorded yet". |
| Manual | At 1280 px and 390 px the title is centred over one column. Run a score on each machine and compare tokens/s per model. |

Correctness is judged by exact arithmetic on seeded rows, the roll-up sum check, and the paid-role tests: hiding the approval card must not remove the server's gate.

## Non-goals

Resuming an interrupted run, scheduling, OpenTelemetry or Langfuse export, alerts, and cost tracking beyond the run ceiling.
