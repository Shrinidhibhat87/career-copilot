# Persistence and Job Lifecycle — Design

> The CV and every decision about a job live in `data/jobs.db` and survive a restart. The CV is versioned by the hash of its text. A job is new until the user has seen it, and the user can save, dismiss, mark applied, restore, or add it to Notion. Core rule: a dismissed job never comes back by accident, and a new CV never forces a rescore. Builds on the `StateStore` port (`ports.py`), `SqliteStore` (`adapters/store/sqlite_store.py`) and the scoring flow in 002-scoring-and-digest.

## Decisions

| Topic | Decision |
|---|---|
| Database | SQLite stays behind the `StateStore` port. WAL mode is on and the busy timeout is explicit (`BUSY_TIMEOUT_S`). No write transaction spans an LLM or network call. |
| CV identity | sha256 of the normalized extracted text. Two exports with the same text are one CV. |
| CV storage | Text, hash, filename, time. The uploaded bytes are never kept. |
| CV history | Rows are never edited. The newest upload is current. Re-uploading an older text makes that version current again. |
| Delete CV | `DELETE /api/resume` removes every version. Scores keep their `cv_sha256`; a hash is not a CV. |
| Legacy file | Removed (amended): `scoring.resume_path` used to be imported once as version 1. The upload is the only source; with no CV, `score` and `run` stop with "No CV is stored yet. Upload one on the CV page." |
| New CV | No rescore. Only jobs scored after the upload use it. `scores.cv_sha256` records which CV scored a job. It is not part of `idx_scores_identity`. |
| New job | A job is new until the user has seen it. Seeing is recorded only when the client posts the ids it showed. A plain GET never marks anything. |
| Top tier | New jobs (a rule-kept job that a model scored) and saved jobs sit first. Applied jobs sort below them. Inside a tier, higher score first. |
| Decisions | `job_decisions` is an append-only log. The current decision is the latest row. A `restored` row counts as no decision. |
| Decision values | `DECISIONS` in `domain/lifecycle.py`: `maybe`, `dismissed`, `saved`, `restored`, `applied`. The API accepts all but `restored`. |
| Reviews | A review label writes its decision in the same transaction: `not_relevant` is `dismissed`, `good_fit` is `saved`, `maybe` is `maybe`. |
| Seen on decide | Any decision also marks the job as viewed. |
| Dismissed | Dismissed jobs leave every list, the digest, and scoring. This is a SQL filter, not a client one. |
| Applied | An applied job stays listed with an "Applied" badge, sorts below new and saved jobs, and is never scored again. The `decision=applied` filter lists only these. |
| Undo applied | `POST /api/jobs/{id}/undo-applied` appends the decision that stood before Applied, or `restored` if there was none. A Notion row stays in Notion. |
| First run | The run keeps going until `scoring.initial_target` (50) matches reach `scoring.min_match_score` (45). Match calls stop at 2x the target on the embedding ranker, 3x on `llm_triage`. A shortfall is reported, never padded. |
| Later runs | The target is `scoring.incremental_target` (6). Earlier matches stay listed. |
| Freshness | `first_seen_at` is the date we trust and is shown as "found N days ago". `posted_at` is information only, because boards disagree on what it means. |
| Recycle bin | A dismissed job waits `lifecycle.bin_days` (default 7, range 1 to 90). The user can restore it or delete it for good. |
| Clean | Cleaning empties `description`, deletes the job's scores, versions and embeddings, and sets `tombstoned_at`. The row stays so dedupe still matches. |
| Never back | `upsert_job` leaves a cleaned job unchanged. `record_decision` refuses it. It cannot be restored. |
| Bin timing | Cleaning runs before each `GET /api/bin`. The bin count in `GET /api/review` leaves out expired jobs. |
| Notion | After Applied is saved, the user is asked once whether to add the job to a Notion "Applications" database. Applied is saved first, and Notion never blocks it. |
| Notion place | The app finds the page `notion.applications_parent_title` ("Career Copilot") shared with the integration, then the database `notion.applications_database_title` ("Applications") inside it, or creates it. |
| Notion key | `NOTION_API_KEY` in `.env`, with Insert content and Update content ticked. |
| No duplicates | `applications.job_id` is unique. A second Yes answers "already". The Notion "Index" is `applications.id`. |
| Notion pace | One Notion call at a time. On HTTP 429 the adapter waits `Retry-After` once, then fails. |
| Accounts | `user_id` columns exist and are always NULL. |

## Shapes

```sql
-- migration 9
CREATE TABLE cv_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, version INTEGER NOT NULL,
  sha256 TEXT NOT NULL, filename TEXT NOT NULL, text TEXT NOT NULL, uploaded_at TEXT NOT NULL,
  is_current INTEGER NOT NULL DEFAULT 1);          -- unique on hash, on version, and on is_current = 1
ALTER TABLE scores ADD COLUMN cv_sha256 TEXT;      -- provenance; NULL = scored before CVs were versioned
-- migration 10
CREATE TABLE job_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, job_id INTEGER NOT NULL,
  decision TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL);   -- latest row per job_id is current
CREATE TABLE job_views (job_id INTEGER PRIMARY KEY, user_id INTEGER, viewed_at TEXT NOT NULL);  -- no row = new
-- migration 19
ALTER TABLE jobs ADD COLUMN tombstoned_at TEXT;    -- NULL = full row
-- migration 20
CREATE TABLE notion_database (id INTEGER PRIMARY KEY CHECK (id = 1), database_id TEXT NOT NULL, page_id TEXT NOT NULL);
CREATE TABLE applications (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL UNIQUE,
  notion_page_id TEXT NOT NULL, created_at TEXT NOT NULL);
```

```python
# config.py
class LifecycleSpec(BaseModel):
    bin_days: int = 7                    # 1..90
class NotionSpec(BaseModel):
    applications_parent_title: str = "Career Copilot"
    applications_database_title: str = "Applications"

# ports.py
class ApplicationLog(Protocol):
    def record(self, entry: ApplicationEntry) -> NotionResult: ...   # entry: index, role, company, source,
                                                                     # pilot_score, why, applied_on, comments,
                                                                     # status, job_url, location
```

```
Notion columns: Role (title) · Index · Company · Source · Pilot score · Why · Date · Comments
                Status (Applied, Interviewing, Offer, Rejected, Withdrawn) · Job URL · Location
```

| Route | Behaviour |
|---|---|
| `POST /api/resume` | Stores a version. Returns `version`, `sha256`, `changed`. 415, 413, 422, 502 on bad uploads. |
| `GET /api/resume`, `GET /api/resume/versions` | The current CV, and `{version, filename, uploadedAt, current}` per version. 404 if none. |
| `POST /api/jobs/viewed` | `{ids}`. Idempotent. Unknown ids are ignored. Bad list is 422. |
| `POST /api/jobs/{id}/decision` | `{decision, note?}`. 404 for an unknown or cleaned job. |
| `GET /api/review` | Rows carry `isNew`, `isSaved`, `isApplied`, `decision`, `firstSeenAt`, `viewedAt`, `scoredWithCv`. `decision` filters to `saved`, `maybe` or `applied`. |
| `GET /api/bin`, `POST /api/jobs/{id}/restore`, `DELETE /api/bin/{id}` | The bin (`days`, `items` of `BinItem`), restore (404 if not in the bin), delete for good. |
| `POST /api/jobs/{id}/notion` | Adds the job to Notion. |

## Flow

```
save_cv(text): hash = sha256(text)
    same hash and current  -> changed = False
    same hash, older       -> make it current
    new hash               -> insert version N+1, clear the old current       # one transaction

score / run: cv = current_cv() or fail "No CV is stored yet"

record_decision(job, d): job missing or tombstoned -> LookupError (404)
    append d; insert job_views row if missing                                  # one transaction

bin: clean_expired(now, days)   # dismissed_at <= now - days -> clean_job
     for each job whose latest decision = dismissed and tombstoned_at is null:
         days_left = max(0, days - whole days since dismissal)
restore_job: not in the bin -> False (404); else append 'restored'
dismiss, restore, dismiss again: the clock reads the latest decision, so it restarts

POST /api/jobs/{id}/notion:       # only after the user says Yes
    if applications has job_id: return "already"
    if no NOTION_API_KEY: 409 "Add NOTION_API_KEY to .env"
    db = saved database id, else search the parent page and find or create the database
    page missing: 409 "Share a page titled 'Career Copilot' with your integration"
    create the row (Why cut to Notion's text limit); other Notion failure: 502 with the reason
    save database id; insert applications(job_id, page_id)
```

If the Notion row is created but the local insert fails, the user can retry from the card. A Notion failure leaves the job Applied and the card shows "Add to Notion".

## Testing

CI runs everything below except the Manual row. Correctness is judged by stored rows and returned lists, never by wording.

| Layer | What it proves |
|---|---|
| pytest `test_cv_store.py` | Same text twice is one row. New text is version 2 and current. Old text becomes current again with no new row. Delete removes all versions. A reopened store still has the CV. |
| pytest store | After a new CV, `jobs_needing_score` returns the same set. A job scored afterwards carries the new `cv_sha256`. Old rows keep theirs. |
| pytest `test_lifecycle_api.py` | Dismissed jobs are absent from review, results, digest and scoring. A `not_relevant` review implies `dismissed`. A GET never marks viewed. The viewed post does. Decide marks viewed. |
| pytest `test_lifecycle_api.py` | New and saved jobs sit above the rest. Applied sorts below them, is not scored, and the `applied` filter lists it. Undo restores the prior decision, or `restored`. |
| pytest `test_recycle_bin_api.py`, `test_recycle_bin_store.py` | Six days gives 1 day left, seven cleans it (injected `now`). A cleaned job has no scores, versions or embeddings and keeps its row. `upsert_job` returns UNCHANGED. Restore of a cleaned job is 404. A second dismissal starts a new period. |
| pytest `test_notion_applications.py`, `test_notion_api.py` | With `httpx.MockTransport`: the database is created with exactly the listed columns, a saved id that is archived is searched again, Index runs 1, 2, 3, a repeat gives "already", 429 then 200 succeeds, 429 twice fails, 401 gives the write-permission message. No key gives 409 and the job stays Applied. |
| pytest score | A run stops at the target above the bar, or reports a shortfall when boards run out. |
| pytest migrations | Migration 19 keeps existing jobs with `tombstoned_at` NULL. Versions are contiguous from 1. |
| vitest `review`, `recycle-bin`, `resume` | The New badge and pin show. Applied asks the Notion question and No sends no call. Delete forever needs the confirm. `/resume` shows the stored CV after a reload. |
| Manual | With a real shared Notion page: Yes creates the database once and three jobs give rows 1 to 3. A cleaned job does not reappear after a crawl. |

## Non-goals

Accounts and per-user tables, syncing Notion changes back, moving off SQLite, and a user-triggered rescore.
