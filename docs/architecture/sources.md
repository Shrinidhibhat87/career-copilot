# Sources and markets

Where postings come from, which ones an account searches, and what happens when a source fails.

Back to the [architecture index](../architecture.md). Designs: specs 017 and 018.

## Two kinds of source

| Kind | Target is | Examples |
|---|---|---|
| ATS board | a company's board slug | Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters, Personio, Teamtailor (`adapters/sources/ats.py`) |
| National board | a search (`was=Python&wo=Berlin`), built from saved preferences | the German federal job board, `jobsuche` (`adapters/sources/jobsuche.py`), free, no account |

The federal board is not a documented API, so a change on their side shows up as a failed
fetch in `source_runs`, never as zero jobs. Each job costs one more throttled request,
bounded by `MAX_JOBS`.

## Markets

A market is a country; its pack (`domain/markets.py`) is code, not config. Each account
enables its own markets in `/preferences`, and the enabled packs decide which searched
sources run and which location and language rules apply. Only `DE` is ready; other
countries are listed as soon. The proposed market comes from the allowed cities first and
the CV second, and is never applied until the person confirms. Saving re-filters stored
jobs. `jobsuche.enabled` in `config.yaml` stays a global off switch.

## Source health

A source is one vendor or service. After 3 failed fetches in a row, or one long
`Retry-After`, all its targets pause for 24 hours (doubling per failed retry, up to 7
days). Then one target is let through: a success closes the breaker, a failure reopens
it. A clean "no such board" is not a failure, and zero jobs is a success.

| Status | Meaning |
|---|---|
| `ok` | fetching normally |
| `degraded` | some failures, still open |
| `paused` | breaker open, with a reason (`unreachable`, `timeout`, `rate_limited`, `forbidden`, `server_error`, `bad_response`) |
| `unknown` | never fetched |

Settings shows each source's status with a Test button (one small request, nothing
written) and Retry now. `career-copilot doctor` prints the stored status. The knobs are
`source_health:` in `config.yaml`; the state is in `source_health` in the account's
`jobs.db`.

## Ads are untrusted text

Before a model reads a posting, `domain/ad_text.py` drops lines aimed at the model
(invisible characters, fake role blocks, "ignore your instructions"). A normal ad is
unchanged; only the prompt copy is cleaned, the stored posting is not edited.
