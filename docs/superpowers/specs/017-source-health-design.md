# Source Health — Design

> Makes a failing job source stop costing requests, and makes its state something the user can see and test: a per-source pause after repeated failures, a status per source in the app, and a test that sends one small request and writes nothing.
> Core rule: a source that keeps failing is paused and says why, and a run never reports a failed source as "no jobs".
> Builds on spec 006 (runs and observability) and spec 009 (source discovery).

## Decisions

| Topic | Decision |
|---|---|
| What exists | Each board already backs off alone: `cooldowns.failures` doubles the wait up to 7 days. This stays as it is. |
| The gap | Nothing looks at a whole source. When a service changes or bans us, every one of its boards is tried again as its own wait ends. A long ban (`Retry-After` over the in-run limit) is remembered only until the run ends. |
| Unit | A source is one vendor or service (`greenhouse`, `workable`, `jobsuche`). The pause covers every target of that source. |
| What counts as a failure | A fetch that raises, except `BoardNotFound` (a clean "no such board", not a sick service). A fetch that returns zero jobs is a success. |
| Opening | After 3 failed fetches in a row across the source's targets (`source_health.open_after`), or at once on a `Retry-After` longer than the in-run limit, for as long as the service asked. |
| Pause | 24 hours (`pause_hours`). The pause doubles after each failed retry, up to 7 days (`max_pause_hours`), the same cap the per-board backoff uses. |
| Retry | When the pause ends, the next run lets one target of that source through. A success closes the breaker. A failure reopens it with the doubled pause. The other targets of that source wait for the result. |
| Reset | Any successful fetch from the source sets the failure count back to 0. |
| Failure reasons | Each failure gets one reason: `unreachable`, `timeout`, `rate_limited`, `forbidden`, `server_error` or `bad_response`. The run view shows it in place of the current line "Source could not be fetched. Try again later." |
| Status | Each source is `ok`, `degraded` (some failures, still open), `paused` or `unknown` (never fetched). |
| Test | A test fetches with the real adapter and reports count, time and reason. It writes no jobs, no cooldown and no breaker state, and a source is tested at most once a minute. |
| Retry now | A button and command that move a paused source to "retry on the next run". It does not fetch anything itself. |
| Where it is kept | In the account's `jobs.db`, with the rest of the run ledger. A ban belongs to the machine's address, not the person, so two accounts on one machine may each learn it. That is accepted. |
| Doctor | `doctor` shows each source's stored status, and keeps the one-request ping for searched sources. It never sends a request per board. |

## Shapes

```python
# domain/source_health.py: pure, no I/O. The same history gives the same state.
class FailureReason(StrEnum):
    UNREACHABLE = "unreachable"; TIMEOUT = "timeout"; RATE_LIMITED = "rate_limited"
    FORBIDDEN = "forbidden"; SERVER_ERROR = "server_error"; BAD_RESPONSE = "bad_response"

@dataclass(frozen=True, slots=True)
class SourceHealth:
    source: str
    consecutive_failures: int
    paused_until: datetime | None
    pause_hours: int
    last_ok_at: datetime | None
    last_failure_at: datetime | None
    last_failure: FailureReason | None
    last_message: str | None          # first 200 characters of the error, no URL query

# adapters/sources/failures.py reads httpx errors, so it sits beside the adapters:
#   read_failure(error) -> FailureRead(reason, retry_after_hours, counts)
# `counts` is False for BoardNotFound. DiscoverRun is given the reader, so it stays free of httpx.
def record(health: SourceHealth, ok: bool, now: datetime, cfg: HealthConfig,
           reason: FailureReason | None = None,
           retry_after_hours: float | None = None) -> SourceHealth: ...
def blocks(health: SourceHealth, now: datetime) -> bool: ...   # true while paused
def status(health: SourceHealth | None, now: datetime) -> str: ...
```

```sql
-- Two migrations. source_health comes with the ledger branch; the two source_runs columns
-- come with the failure-reasons branch.
CREATE TABLE source_health (
    source               TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    paused_until         TEXT,
    pause_hours          INTEGER NOT NULL,
    last_ok_at           TEXT,
    last_failure_at      TEXT,
    last_failure         TEXT,
    last_message         TEXT
);
ALTER TABLE source_runs ADD COLUMN reason      TEXT;
ALTER TABLE source_runs ADD COLUMN duration_ms INTEGER;
```

```
GET  /api/sources/health               → [{source, status, targets, lastOkAt, lastFailure, lastMessage,
                                           pausedUntil, fetchedLast7Days}]
POST /api/sources/{source}/test        → {ok, fetched, durationMs, reason?, message?}
POST /api/sources/{source}/resume      → {status}
career-copilot sources test [SOURCE]   career-copilot sources resume SOURCE
```

```yaml
# config.yaml
source_health: {open_after: 3, pause_hours: 24, max_pause_hours: 168}
```

## Flow

1. `DiscoverRun` reads the source's health before each target. While it is paused, the target is skipped as `source:identifier (source paused until …)`, which already appears in the run's skipped list.
2. After a fetch, the run calls `record`. A failure stores the reason and the time the call took. A success clears the failure count and closes the breaker.
3. When a pause has ended, the first target of that source in the run is the retry. Later targets of the same source in that run are skipped until it answers.
4. A host's long `Retry-After` is read from the same 429 that already makes `HostThrottle` block the host in memory. It now also opens the breaker for that long, so the next run does not ask again.
5. The Settings page lists each source with its status, last success, last failure reason and message, and "paused until". Test and Retry now sit on each row. A test shows its result in the row and changes nothing else.
6. `career-copilot doctor` prints one line per source from the stored state (`ok`, `warn … paused until …`). Searched sources keep their one-request ping.

## Testing

- Pure state machine, no I/O: opens on the third failure in a row, not on two. A success resets the count. `BoardNotFound` never counts. A long `Retry-After` opens at once for that long. A failed retry doubles the pause, up to the cap. `read_failure` maps each exception type to one reason.
- `DiscoverRun` with fake sources: three failing targets of one source pause the fourth; the pause skips are listed; after the pause exactly one target is tried; one success reopens the rest. A different source is unaffected.
- Store: the migration adds the table and columns on an existing database. Health round-trips.
- API contract tests with the test client: health shape, test writes nothing (job, cooldown and health rows unchanged), the one-a-minute limit answers 429, resume only changes a paused source.
- By hand, after the UI branch: start `career-copilot ui` and open `/settings`. Each source shows a status. Press Test on one source and see count and time. Break one source (a wrong host in a copy of the config), run `discover` three times, and see it move to paused with a reason; press Retry now and see the next run try it once.

## Non-goals

Removing or disabling boards automatically, sending an alert when a source pauses (spec 020 covers notifications), a breaker shared across accounts, proxies or address rotation, changing the per-board backoff, and probing every board from `doctor`.

## Delivery

One spec, five small branches, merged in this order:

1. `feat/source-health-ledger` (pure state machine, migration, breaker in `DiscoverRun`, config)
2. `feat/source-failure-reasons` (reason and duration on `source_runs`, run view shows the real reason)
3. `feat/source-health-api` (health, test and resume endpoints, and the CLI commands)
4. `feat/source-health-ui` (the Settings section with Test and Retry now)
5. `feat/doctor-source-health` (stored status lines in `doctor`)
