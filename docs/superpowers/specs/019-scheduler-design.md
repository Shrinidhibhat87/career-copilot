# Scheduler with Catch-up — Design

> Makes the daily refresh happen without a click: each account sets the times of day it wants a run, and a run that was due while the app was closed or asleep happens once when the app is back.
> Core rule: a scheduled run is a normal local run that never spends money, never overlaps another run, and never repeats for a slot it already covered.
> Builds on spec 006 (runs), spec 015 (accounts) and spec 017 (source health).

## Decisions

| Topic | Decision |
|---|---|
| The gap | Runs start only from a click, the CLI, or `request_refresh` when the app opens with results older than 20 hours. Nothing runs at a chosen time, and nothing makes up for a closed laptop. |
| Where it lives | A ticker thread inside the `ui` server, started at sign-in and stopped at sign-out. It checks once a minute. No second process, no OS service. |
| Whose runs | The signed-in account only. Passcodes are held in memory, so a server nobody has signed in to has nothing to run for. Said on the page. |
| Per account | `enabled` and a list of local times (`HH:MM`, 1 to 4), kept in the account's `jobs.db`. Off until the person turns it on. Machine local time; no timezone field. |
| Due | The latest slot at or before now, looking back over today and yesterday. It is covered when any run started at or after it, whatever its outcome. A failed run is not retried within the same slot. |
| Catch-up | The same rule at sign-in and on every tick: the latest due slot not yet covered starts one run. Three missed days give one run, not three. |
| What starts | `request_refresh`, unchanged: an incremental run with no cost preview, only when no role is billed, a CV exists and the model backend is up. A billed role never runs unattended. |
| Overlap | Skipped when a run is active, in the browser or the terminal. Not queued: the next tick sees it. `request_refresh` gets `queue=False` for this. |
| Skips | A skip (billed role, no CV, backend down, run active) is remembered in memory with its reason and shown on the page. The same reason is retried after 15 minutes, not every minute. |
| Stuck runs | Already handled: a dead owner's `running` row is reclaimed at sign-in and by `begin_run`. This spec adds no recovery code. |
| History | A run records its `trigger`, `manual` or `scheduled`. The run history and `GET /api/runs` show it. |
| Sleep | The ticker measures wall-clock time, so a machine that wakes after its slot runs it on the first tick. |
| Doctor | One line: `schedule: 08:00, 18:00 (on) | off`. |

## Shapes

```python
# domain/schedule.py: pure, no I/O.
@dataclass(frozen=True, slots=True)
class Schedule:
    version: int
    enabled: bool
    times: tuple[str, ...]        # "08:00": local, sorted, unique, at most 4

def due_slot(times: Sequence[str], now: datetime) -> datetime | None: ...   # latest slot <= now, today or yesterday
def next_slot(times: Sequence[str], now: datetime) -> datetime | None: ...
def is_covered(slot: datetime, last_run_started: datetime | None) -> bool: ...
```

```sql
-- Migration 35: versioned like market_settings; the newest row is in force.
CREATE TABLE schedule_settings (
    version INTEGER PRIMARY KEY AUTOINCREMENT,
    enabled INTEGER NOT NULL,
    times   TEXT NOT NULL,            -- JSON list
    saved_at TEXT NOT NULL
);
ALTER TABLE runs ADD COLUMN trigger TEXT NOT NULL DEFAULT 'manual';
```

```
GET /api/schedule → {enabled, times, next, lastRunAt, lastSkip: {reason, at} | null, canRun: bool, whyNot: str | null}
PUT /api/schedule {enabled, times} → same shape; 422 on a bad time, none, or more than 4
```

## Flow

1. `/settings` has a Schedule section: a switch, up to four times, the next run, the last skip, and a note that the app must be open and signed in.
2. The ticker wakes each minute and reads the schedule. Off or no slot due: nothing.
3. A due slot that is not covered, with no run active, calls `request_refresh(config, trigger="scheduled", queue=False)`.
4. A skip is stored in memory with its reason. Sign-out stops the ticker, and the next sign-in runs the same check once at start, which is the catch-up.
5. A run started this way is labelled `scheduled` in the run history.

## Testing

- Pure: `due_slot` across midnight, before the first slot of the day, an empty list; `next_slot`; `is_covered` at the boundary; times validated and sorted.
- Store: migration 35 on an existing database, versions round-trip, newest wins; `trigger` defaults to `manual`.
- Ticker, with a fake clock and a fake `request_refresh`: starts once per slot, not again after a failed run, catches up once after three missed days, skips while a run is active without queueing, retries a skip after 15 minutes only.
- Billed role: no run starts, and the page says why.
- API contract: shape, validation errors, `PUT` takes effect on the next tick.
- By hand, after the UI branch: set a slot two minutes ahead on a copy of a real account; a run appears in `/settings` history labelled scheduled; set a slot in the past with no run since and sign in again: one catch-up run starts.

## Non-goals

Running with nobody signed in, an OS service or cron installer, a timezone field, weekdays or intervals, retrying a failed run, scheduled runs on billed models, a scheduled digest (spec 020), and staggering several accounts.

## Delivery

One spec, three branches, merged in this order:

1. `docs/scheduler-spec` (this spec)
2. `feat/scheduler` (pure module, migration 35, store, `trigger`, ticker, API, doctor line)
3. `feat/scheduler-ui` (Schedule section on `/settings`, `scheduled` label in the run history)
