# Pipeline and data

How jobs are collected, deduplicated, filtered, scored and stored.

Back to the [architecture index](../architecture.md).

## Collection ladder

Cheap first, stop when satisfied. Targets are `source_targets` rows (seeded from
`targets:` in `config.yaml`, extended by company-list import and approved discovery
proposals), sorted by `tier`, then by how long each board has waited: a board never
visited goes first, and a visit sends a board to the back of its tier, so a run that stops
early picks up where the last one left off.

| Input | Source | Cost |
|---|---|---|
| `targets:` / `source_targets` | ATS board APIs, 8 vendors, public, no login | free |
| Company lists | Notion database or CSV, resolved by deterministic board probing | free |
| `DiscoveryAgent` | web search + ATS probe, proposes boards | LLM + search, budget-gated |

The run stops once `stop_after` new qualifying jobs are found (`initial_target` while
the store is empty); one target adds at most `max_jobs_per_target` postings. A target
in cooldown is skipped; a failing fetch is recorded in `source_runs` and gets a longer
cooldown, never counted as zero jobs. A host that answers 429 is throttled per host.

## Dedupe

Four stages, enforced by DB constraints, not in-memory checks:

1. `(source, source_job_id)` unique
2. canonical URL, tracking params stripped
3. `fingerprint = sha256(company_norm | title_norm | location_norm)`, company aliases resolved
4. `content_hash` of the normalized description → change detection

A repeat sighting updates the row and logs the change in `job_versions`. A job unseen
for `inactive_after_days` goes `inactive`; the crawl never deletes. A job the user
dismisses goes to a recycle bin and is cleaned for good after `lifecycle.bin_days`.

## Scoring

Hard filters run first with no LLM: role gate, stack terms, location, language,
seniority, employment type, eligibility, driven by the saved preferences (the
`filters:` block only seeds them). Unclear data goes to `manual_review`, never a silent
reject.

Survivors are ordered by the `ranker`: `embedding` (default; `Embedder` over
`JobVectorStore`, fastembed locally) or `llm_triage`, which it falls back to. Only the
top N reach match, capped per company by `max_per_company`; a match counts toward the
target at `scoring.min_match_score`. Match output is schema-validated, retried once, and
every score row carries `rubric_version`, model and `preferences_hash`. A job is
rescored only when `content_hash`, rubric, model or preferences change; a new CV alone
does not. A hard-filter reject can never be outweighed by a high score.

## Data model

One SQLite file per account, `data/users/<name>/jobs.db` (spec 015), with numbered append-only migrations in
`adapters/store/migrations.py` (recorded in `schema_migrations`).

| Group | Tables |
|---|---|
| Jobs | `jobs`, `job_versions`, `job_embeddings`, `job_outlines`, `company_aliases` |
| Scores | `scores`, `reviews`, `corrections`, `feedback_entries` |
| Lifecycle | `job_decisions`, `job_views`, `applications`, `notion_database` |
| Sources | `source_targets`, `source_runs`, `source_proposals`, `cooldowns`, `import_companies` |
| Profile | `cv_versions`, `preferences`, `intent_entries`, `sample_roles`, `search_profiles` |
| Runs | `runs`, `llm_calls`, `llm_daily`, `tool_calls`, `tool_daily`, `app_sessions` |

`runs` and `source_runs` record every attempt with success, count, and error text, so a
silently failed scrape is never read as "no jobs today".

## Application tracking

Saving (✓), dismissing (✕) and marking applied are decisions in `job_decisions`;
`applications` holds the applied jobs. When a Notion Applications database is
configured, `ApplicationLog` mirrors each applied job there. The same Notion
integration can also feed a company watchlist.
