# Review and Recommendations — Design

> Retrieval keeps the right postings and the Recommendations page shows them ranked, one list and one card to page through. Core rule: a posting is found however its board spells the place, a title that fits no role is parked and never dropped, and the page shows what helps the user decide, not the machinery. Builds on 001-foundation (hard filters, `jobs`), 002-scoring-and-digest (triage and match scores), 005-preferences (`title_terms`) and 006-runs-and-observability (refresh). Saved, dismissed and Applied jobs are in 010-persistence-and-lifecycle; the chat is in 011-agentic-recommendations.

## Decisions

| Topic | Decision |
|---|---|
| A location | A set of places, not one token. `location_segments` splits on `,` `;` `\|` `/` and a spaced hyphen. |
| Cleaning a place | Per segment: lowercase, drop punctuation, drop pure country or region words (germany, deutschland, de, europe, eu, emea, worldwide, global), apply aliases, map remote hints to `remote`, drop repeats. |
| Location verdict | The job passes when any segment is allowed. The other segments become the warning "also listed: ...". |
| Stored place | `location_norm` stays the first segment, so display, grouping and `fingerprint` keep their shape. The rules re-read `posting.location`, so there is no column for segments. |
| Re-seen postings | A changed fingerprint still matches the old row by source id or URL, so the row is updated, not duplicated. |
| Role gate | `title_terms` on `Preferences` and `FilterConfig`, matched against the title only. `excluded_role_terms` still rejects. |
| Unknown title | A title that matches no term goes to `manual_review` ("title outside the stated role families"), never `reject`. An empty list turns the gate off. |
| Missing terms | A saved preferences payload without a `title_terms` key gets `DEFAULT_TITLE_TERMS` (the `FilterConfig` defaults). A key saved empty is a deliberate "no gate". |
| Seed terms | Specific role phrases (data engineer, mlops, site reliability engineer, ...). Never the bare word "engineer". |
| Run bound | `stop_after` applies to every tier. `max_jobs_per_target` (100) cuts one board's list before filtering, and the source ledger records the bounded count. |
| Rank | `review_page` ranks by the score that exists: match, else triage, else none. A location-mismatch role is capped at 9. Stored scores are untouched. |
| Kept | Rules kept it (`pass` or `manual_review`) and its score is at or above the threshold. An unscored job is not kept. The counts `kept`, `dropped`, `below`, `unscored`, `total` say what the cut hides. |
| Saved always show | Saved and applied jobs stay listed whatever the threshold. Dismissed jobs never appear. |
| Size | `limit` is 10 by default, at most 50. The page steps 10, 25, 50 with "Show more". |
| Labels | `reviews` and `corrections` are append-only. The latest review per job is the current label. A label also writes a decision (010). Neither re-ranks anything. |
| Stale label | `content_hash` is stored with each label. `labelStale` is true when the advert changed after it. |
| Name and route | The page is `/recommendations`. `/review` is a stub that calls `router.replace("/recommendations")`, because the static export has no redirects. Step 4 of the header is RECOMMENDATIONS. Settings is outside the steps. |
| Top line | "N recommendations" counts rows at or above the threshold. No tally of above, below and unscored. |
| Lower matches | "Show lower matches" adds below-threshold rows tagged "Lower match". Unscored rows are never listed, unless saved or applied. |
| Controls | List and Card toggle, a "Show scores >=" slider from 30 to 90 in steps of 5, "Update CV", "Edit preferences". |
| Refresh | "Look for new jobs" calls `POST /api/runs/refresh`, shows "Searching..." while the run is active, refetches, then says "Found N new" or "Nothing new found". If no run starts it shows the server's reason. New means ids not listed before. |
| Company | The name sits under the title and links in a new tab when `companyUrl` is set. Else it is plain text. |
| Company link | The imported homepage (`import_companies.homepage_url`), else the board's careers page from `careers_url`, else `null`. |
| Location mismatch | A remote role posted from another country is tinted red and tagged "Location mismatch". |
| Freshness | The row shows its age and "last seen". "MAY BE CLOSED" shows once `lastSeenAt` is older than `inactive_after_days` (21). |
| Card | The posting's Responsibilities and Requirements (see Outline) and the link, the score (donut when `parts` exist, see 002), "Why it may fit", "Gaps", three verdict buttons. No rule badges, rubric or cost. An unscored job shows "No match claims — decided by rules." |
| Outline | A posting opens with the company's pitch and boards name their sections differently, so a model (the `match` role) copies out the responsibilities and requirements on the card's first open, at most 6 each. Each sentence must appear in the stored description or it is dropped; if none survive, or the call fails, the card says the summary is not working and points to the original posting. Kept in `job_outlines` (migration 30) while `content_hash` is unchanged. The stored description, scoring and filters are untouched. |
| Card panes | Both columns carry a background so they read as the same height, however much text each holds. |
| Evidence | Each claim's CV quote sits in a closed "From your CV" disclosure. "Evidence is wrong" stores a `corrections` row (`kind = "evidence"`) and becomes "Noted". |
| Confirmations | Good fit, Not relevant and Maybe each highlight and say what happened. Maybe keeps the job listed. |
| Card paging | Arrows with "n / N" wrap round. With one card both are disabled. |
| Cursors | Disabled buttons use `not-allowed`. `wait` only while a request is in flight (`aria-busy`). |

## Shapes

```python
# domain/normalize.py
def location_segments(location: str) -> tuple[str, ...]: ...   # "Munich - Berlin" -> ("munich", "berlin")
def normalize_location(location: str) -> str: ...              # first segment, or ""

# adapters/store/sqlite_store.py
def review_page(self, threshold, include_below=False, limit=10, decision=None) -> dict
def job_detail(self, job_id) -> dict | None     # claims, gaps, evidence, filter reasons, label, labelStale
def record_review(self, job_id, label, note, now) -> int
def record_correction(self, job_id, kind, target, note, now) -> int

# adapters/api/review.py
def company_url(store, company, source, job_url=None) -> str | None
```

```sql
-- migration 5: append-only, no UPDATE, no DELETE, no unique index
CREATE TABLE reviews (id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
  label TEXT NOT NULL,          -- good_fit | maybe | not_relevant
  note TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE corrections (id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs (id) ON DELETE CASCADE,
  kind TEXT NOT NULL,           -- evidence | rule | batch (a null job_id is a batch note)
  target TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
-- migration 17
ALTER TABLE import_companies ADD COLUMN homepage_url TEXT;   -- Notion URL property or CSV column; never an ATS URL
```

```
GET  /api/review?threshold=30..90&includeBelow&limit=1..50&decision=saved|maybe|applied
       -> {items, threshold, limit, kept, dropped, below, unscored, total, binCount, inactiveAfterDays}
       each item: score, scoreStage, why, parts, label, labelStale, belowThreshold, locationMismatch, companyUrl, ...
GET  /api/jobs/{id}              -> one card (404 if unknown)
POST /api/jobs/{id}/outline      -> {status: "ok", responsibilities[], requirements[]} | {status: "unavailable"}   (404 if unknown)
POST /api/reviews                {jobId, label, note} -> 201, "rerankedNothing": true
POST /api/corrections            {jobId, kind: evidence|rule, target, note} -> 201
```

## Flow

```
segments(loc):  split -> per piece: clean, drop country words, alias, remote hint -> unique, in order

check_location(posting):
    segs = segments(posting.location)
    none               -> manual_review "location missing"
    some allowed       -> pass; warn "also listed: <others>"; a flagged city still warns
    only remote, not allowed -> reject "remote not allowed by config"
    posting.remote and allow_remote -> pass with warning "remote role with non-German location"
    else               -> reject "relocation required" or "location outside Germany"

check_role_family(title):
    excluded term hit -> reject
    title_terms set and no hit -> manual_review
    else pass

discover: for target in targets by tier, then least recently visited:
    if qualified >= stop_after -> skip "(target met)"         # any tier
    jobs = fetch(target)[:max_jobs_per_target]; record the bounded count

review_page(threshold, include_below, limit, decision):
    score = match, else triage, else none; if location mismatch and score > 9: score = 9
    visible = active and not dismissed and (kept or saved/applied)   # include_below drops the kept test
    order = new/saved first, then score desc (none last), then last seen
    return up to limit rows with the counts

company_url(company, source, job_url):
    homepage for the company name -> else careers_url from the posting's board hint,
    else from a resolved import row -> else None

card paging: next = (i + 1) % n, prev = (i - 1) % n
refresh: started? -> poll the run until it is not running/syncing -> refetch -> count ids not seen before
         not started -> show reason
```

## Testing

CI runs `uv run pytest` and `npm test`. The deterministic filters cost nothing and run on fixtures.

| Layer | What it proves |
|---|---|
| `tests/unit/test_normalize.py` | "Berlin, Berlin, Germany" gives `("berlin",)`. Multi-city strings split. `normalize_location` is the first segment. |
| `tests/unit/test_filters.py`, `test_filters_preferences.py` | One allowed place keeps a posting. A title with no term goes to `manual_review`, an excluded one is rejected. An empty `title_terms` disables the gate. |
| `tests/unit/test_preferences.py` | A payload without `title_terms` gets the defaults. An empty saved list stays empty. |
| `tests/e2e/test_discover_run.py` | The target stops a free tier-0 board. One board cannot be the whole run. |
| `tests/unit/test_apply_preferences.py` | Re-running the rules over stored jobs reports transitions with zero model calls and writes no score row. |
| `tests/unit/test_outline.py`, `tests/contract/test_outline_api.py` | Only sentences found in the posting survive, glued or broken text still matches, nothing grounded is a failure. One model call per posting, then served from `job_outlines`; a failure is reported as `unavailable` and not kept; a changed posting is read again. |
| `tests/contract/test_review_api.py` | The cut, the counts, the limit, unscored shown as unscored, the card payload, 404s, labels appended and read back, bad label refused, `companyUrl` order, mismatch capped below 10. |
| vitest `review`, `review-card`, `refresh`, `legacy-review-route` | No rule or rubric text. Lower matches tagged, unscored never listed. Company link opens in a new tab with `noopener noreferrer`. Arrows wrap and show "n / N". Evidence hidden until opened, "Noted" posts one correction. Refresh counts only new ids. `/review` replaces itself. |
| vitest `cursor` | A disabled control never gets `cursor: wait`. |
| Manual | On a copy of `data/jobs.db` no Berlin posting is rejected for location, and Credit Risk Manager, Financial Accountant, IT Support Specialist and Executive Briefing Center Lead leave the pass set. Click through every card button. |

Correctness is judged by re-running the filters over stored rows and reading the outcome transitions, and by exact assertions on list counts and order.

## Non-goals

Re-ranking from stored labels, new job sources, changing scoring or thresholds, and drag-to-decide.
