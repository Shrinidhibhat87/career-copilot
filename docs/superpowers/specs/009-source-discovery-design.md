# Source Discovery — Design

> Two ways a job board enters the crawl, over eight ATS vendors. A curated company list (Notion or CSV) is trusted and goes straight in. A discovery agent guesses boards and only proposes them for the user to approve. Core rule: HTTP decides what is a real board, never a model, and `config.yaml` is never rewritten by a machine. Builds on the `JobSource` adapters in `adapters/sources/ats.py`, the persistence layer (010-persistence-and-lifecycle), the runs ledger (006-runs-and-observability) and local models (008-local-model-runtime).

## Decisions

| Topic | Decision |
|---|---|
| Vendors | Eight boards, all public and without a login: `greenhouse`, `lever`, `ashby`, `personio`, `teamtailor`, `smartrecruiters`, `recruitee`, `workable`. A vendor is a class in `ats.py` registered in `SOURCE_TYPES`; no config or schema change. |
| Probe order | `SOURCE_TYPES` order is the order the resolver probes a slug, most common first. `workable` is last. |
| XML vendors | `personio` and `teamtailor` publish XML. `http_fetcher` returns text when the content type says xml, JSON otherwise. |
| Not found | A clean 404 is "no board". `workable` with no name and no jobs, and `smartrecruiters` with `totalFound` 0, raise `BoardNotFound`, which `probe_board` reads as the same clean miss. Crawling a known empty board returns no jobs. |
| Throttle | `http_fetcher` spaces requests per host. `apply.workable.com` gets at least 1.0 s between requests, across threads (`HostThrottle`). A 429 is retried twice, honouring `Retry-After`. A wait over 10 s blocks the host for the rest of the run and later requests fail as 429 without being sent. |
| Trust: list | A company list is the user's own pick. A resolved company is crawled on the next run with no approval. |
| Trust: agent | Agent output is a proposal. Only `sources review --approve` moves it into the crawl. A rejected proposal is never proposed again. |
| Crawl set | `crawl_targets` returns the `config.yaml` `targets:` first, then every active row in `source_targets` (approved or imported). |
| No LLM on import | Resolution is slug-guessing against the real board APIs. No model, no search, no cost. |
| Company | A company is `CompanyRef(external_id, name, url)`. Other columns in the source are never read. Names are trimmed, one wrapping bracket layer is stripped, spaces collapse (`normalize_company_name`). A name that is a URL is used as the URL. |
| One list | One list at a time, from `company_lists:`. Notion wins if both are set. |
| Notion read | The database title property is the name. The first `url` property is the URL. Pages of 100. API version pinned to `2022-06-28`. Needs `NOTION_API_KEY` in `.env`, and the database shared with the integration. The token never goes to the frontend. |
| CSV read | Columns `name` and optional `url`. `external_id` is a hash of the normalised name. |
| Resolve | Try a board URL hint first. Then probe each slug variant (name, trailing legal or descriptor words peeled one at a time, hyphenated, company domain) against every vendor. A board with open roles wins; an empty board is a fallback. |
| `resolved` | A board was found. |
| `unresolved` | Every probe was a clean miss. `last_error` lists the slugs tried. |
| `blocked` | A probe got a non-404 status, a timeout, a connection error or a bad body. A board may exist but could not be checked. `last_error` holds the reason. |
| Ledger | `import_companies` is keyed on `(list_source, external_id)`; a sync upserts it. A company gone from the list becomes `inactive` and its `source_targets` row is switched off, never deleted. Collected jobs stay. |
| Recheck | A company probed in the last 24 hours with status resolved or unresolved is skipped. A blocked one is always probed again. `--refresh` probes all. A stale resolved row first rechecks only its own board. |
| Manual board | The user can name a board for an unresolved or blocked company by `vendor/slug` or a careers link. It is probed first. A wrong answer changes nothing. On success the row is `pinned`: later syncs refresh its role count but never replace it. |
| Pinned and gone | A pinned board that disappears is `blocked` with the old choice kept, so it recovers when the board returns. |
| Visible | One sync gives one set of rows, shown in the CLI output, the digest section `## Watchlist coverage` (omitted when empty), and the run's `sourceFailures` on the runs screen. |
| Connect UI | `CompanySources` (`components/company-sources.tsx`, in the resume step) saves a Notion database id or uploads a CSV. Saving only stores the connection; the sync starts when the user continues to preferences. `CompanySummary` shows counts and `UnfoundCompanies` lets the user set a board for a missing company. |
| Sync run | A sync is a `runs` row of kind `company_sync`. Counts go to `record_company_sync`. |
| Agent | `agents/discovery/agent.py` is a `pydantic_ai.Agent` with five tools. It is the only open-ended step: the number of searches is not known up front. |
| Tool refusal | Every cap returns text to the model ("refused: ..."), not an exception, so the agent ends cleanly with the proposals it has. |
| Search key | `sources discover` needs `TAVILY_API_KEY`. Without it, it stops before any model call. |
| Model check | The `discovery` role must be reachable and not report `tools: False`, or the command stops. Without its own role it uses the `triage` model. |
| Bounds | `discovery:` config: `target_proposals` 3, `max_searches` 6, `max_eur_per_run` 0.25, `max_requests` 12, `max_tool_calls` 24, `max_seconds` 600. `UsageLimits` bounds requests and tool calls, `CostMeter` bounds euros, and a wall-clock timeout wraps the run. |
| Owner guard | `web_search` refuses a query holding the resume owner's name, email or phone. The check is code, not prompt. |
| Proposal rules | `propose_source` refuses a board not probed successfully in this run, one already known or decided, a repeat in the run, or empty `evidence`. |
| Dedupe | `known_companies` lists decided boards and stored companies. `(board_type, slug)` is unique in `source_proposals`. |
| Approval | Approve is a status change plus a `source_targets` insert in one transaction. Reject changes status only; the row stays. |
| Dry run | `--dry-run` finds boards and stores nothing. |

## Shapes

```sql
-- migration 6
source_proposals (board_type, slug, company, evidence, confidence, job_count,
                  sample_titles, status /* pending | approved | rejected */, run_id,
                  proposed_at, decided_at)       -- unique (board_type, slug)
source_targets   (source, identifier, tier, cooldown_hours, active, added_at,
                  from_proposal_id)              -- unique (source, identifier)
-- migration 7: import_companies, plus source_targets.origin ('approved' | 'imported')
-- and source_targets.external_id
import_companies (list_source, external_id, name, status /* unresolved | resolved | blocked | inactive */,
                  board_type, slug, last_error, first_seen_at, last_seen_at)
-- migration 8: last_probed_at, open_roles   migration 11: pinned   migration 17: homepage_url
```

```python
class CompanyListProvider(Protocol):          # ports.py: Notion and CSV adapters
    def fetch(self) -> Sequence[CompanyRef]: ...

class ImportSourcesReport:                    # usecases/import_sources.py
    fetched: int; resolved: tuple; unresolved: tuple
    blocked: tuple[tuple[CompanyRef, str], ...]; deactivated: tuple[str, ...]
```

Agent tools (`agents/discovery/tools.py`): `web_search` (paid, capped), `probe_ats`, `sample_jobs`, `known_companies`, `propose_source` (free).

CLI (`adapters/cli_sources.py`): `sources discover [--target --limit-searches --budget --dry-run]`, `sources review [--approve ID | --reject ID]`, `sources list`, `sources sync [--from notion|csv --refresh --dry-run]`, `sources set-board COMPANY BOARD [--from]`. `sources list` labels each board `config`, `approved` or `imported`.

API (`adapters/api/company_lists.py`): `GET/PUT /api/company-lists/config`, `GET /api/company-lists/summary`, `POST /api/company-lists/csv` (.csv, 5 MB), `POST /api/company-lists/sync`, `POST /api/company-lists/companies/{id}/board`.

## Flow

```
sync(list_source, refresh):
    no list configured, or notion without NOTION_API_KEY -> stop before any request
    refs = provider.fetch()                       # Notion error or unreadable file -> run fails, message shown
    for ref in refs: upsert import_companies; skip if probed < 24h and resolved/unresolved
    probe the rest on 8 worker threads (store writes stay on the calling thread):
        pinned          -> recheck its board only; gone -> blocked, choice kept
        resolved, stale -> recheck its board; gone -> resolve again
        else            -> resolve_company(ref)
        record status; resolved -> insert source_targets(origin="imported", tier 0, cooldown 48h)
    absent from refs -> inactive, source_targets.active = 0
    finish run with counts

discover(target, budget):
    no TAVILY_API_KEY, or discovery model down or without tools -> stop, nothing built
    agent loop under UsageLimits + CostMeter + timeout:
        known_companies -> web_search -> probe_ats (404 or timeout = no board, not an error)
        -> sample_jobs -> propose_source (needs a probe in this run, plus evidence)
        a cap or budget refusal -> agent summarises and ends; proposals so far are kept
    store new proposals as pending; slugs already stored count as duplicates

review --approve ID: pending -> approved + source_targets row, one transaction; not pending -> error
```

## Testing

| Layer | What it proves |
|---|---|
| CI golden (`tests/golden/test_ats_parsers.py`) | Each vendor's recorded payload maps to jobs; empty and unknown boards behave as above; `HostThrottle` spaces one host only; a long 429 blocks the host; a short 429 is retried. |
| CI unit (`test_company_resolution.py`) | URL hints for every vendor, slug variants, clean 404 is `unresolved`, other failures are `blocked`, SmartRecruiters `totalFound` 0 is unresolved, a board with roles beats an empty one. |
| CI unit (`test_discovery_tools.py`) | Owner-detail queries, the search cap and the euro cap refuse as text. A proposal needs a probe, evidence and a new board. |
| CI contract (`tests/contract/`) | Provider contract on CSV and a fake; Notion against recorded pages; `import_companies` upsert and deactivate; proposals unique index and promotion; company-list API routes. |
| CI e2e (`test_import_sources.py`) | A scripted two-run sync: resolve, block, deactivate, no re-probe inside 24 hours, `--refresh`. |
| CI e2e (`test_set_board.py`) | A hand-set board is pinned, survives refresh, and a wrong answer changes nothing. |
| CI e2e (`test_discover_sources.py`) | A scripted model run (`FunctionModel`) proposes only probed boards, never changes the crawl, stops cleanly on caps and the wall clock, and refuses without a search provider. |
| CI frontend (`src/frontend/tests/`) | `company-sources.test.tsx` and `unfound-companies.test.tsx` cover connecting a list and setting a board. |
| Manual | A live Notion sync and a live `sources discover` run. Vendor probing is never run in a burst. |

Correctness is judged against recorded board payloads and a scripted model. CI makes no network call.

## Non-goals

No job-level aggregators, no write-back to Notion, no multi-list import, no Notion OAuth connect button, no login-gated scraping, no work around anti-bot measures.
