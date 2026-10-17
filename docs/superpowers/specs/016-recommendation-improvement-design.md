# Recommendation Improvement — Design

> Makes recommendations stricter, makes them learn from what the user saves and dismisses, stops a dismissed job from coming back, and lets the user find a company's job board by typing its name.
> Core rule: a ✕ is final for that job and for its reposts, and every ✓ or ✕ teaches the next run.
> Builds on spec 002 (scoring and digest), spec 009 (source discovery) and spec 010 (persistence and lifecycle).

## Decisions

| Topic | Decision |
|---|---|
| Score bar | The default `scoring.min_match_score` goes back to 45. The config comment and the docs say the same number. |
| Ranking | The ranker already orders jobs by how close they are to sample roles and the user's intent. It now also counts how close each job is to the CV. |
| Learning | Saves and dismisses are boiled down to a short written note ("what this person likes, what they do not"). The match step reads that note along with the CV. |
| Note timing | The note is built at the start of every score run from the newest ✓ and ✕ (up to eight of each). It is plain text assembled in code, with no model call, so it costs nothing and cannot fail. It is not stored: the same decisions always give the same note. |
| Reasons | A ✕ can carry one optional reason: wrong role, seniority, location, company or other. Reasons go into the note. A ✕ without a reason still counts. |
| Reposts | A new posting counts as a repost when it is from the same company and its description is close to a dismissed job's description. It is hidden the same way as the original. |
| How long | A dismissal follows reposts for good. There is no expiry. |
| Digest | The manual-review list in the digest hides dismissed jobs, like every other list. This is a bug fix. |
| Name lookup | The user types a company name. The app tries the usual board names on all eight job platforms, shows what it found, and adds the board only after the user confirms. |
| Web search | If nothing is found, the app asks before running the discovery agent. The agent runs once for that company, inside the existing `discovery:` budget. |
| Company search | The job lists get a search box that filters by company name. |

## Shapes

```python
# 1. Ranking: one more term. The weight lives in config, not in code.
rank = (1 - cv_weight) * (0.6 * best_exemplar_similarity + 0.4 * best_intent_similarity) \
     + cv_weight       * cv_similarity   # skipped when no CV is stored or cv_weight is 0

# 2. Learned note: built from decisions each run, never stored. Titles and companies only.
#    Old scores are not redone when the note changes (as with a new CV).
def build_preference_note(signals: Sequence[DecisionSignal]) -> str: ...

# 3. Dismiss reason: one nullable column on the decision row.
#    job_decisions.reason  TEXT NULL  -- wrong_role | seniority | location | company | other

# 4. Repost link: so the user can see why a job is hidden.
#    jobs.repost_of  INTEGER NULL REFERENCES jobs (id)
```

```text
# Config (new keys, defaults shown)
scoring:
  min_match_score: 45
  cv_weight: 0.3             # share of the ranking score; the rest is the existing blend
dedupe:
  repost_similarity: 0.90    # tune on real dismissed jobs before shipping
```

```text
# Name lookup API
POST /api/sources/lookup   { "name": "Acme GmbH" }
  -> { found: true, vendor: "ashby", slug: "acme", openRoles: 12, sampleTitles: [...] }
  -> { found: false, triedSlugs: [...], canSearchWeb: true }
POST /api/sources/lookup/confirm   { vendor, slug }       # adds the board
POST /api/sources/lookup/search    { "name": "Acme GmbH" }  # runs the agent, same answer shape
```

## Flow

```text
score run:
  note = build_preference_note(newest saved and dismissed jobs, with reasons)
  rank candidates with the CV term included
  match each job with: CV + preferences + note
  count a match toward the target only if score >= min_match_score (45)

new job arrives (upsert finds no existing row):
  candidates = dismissed jobs of the same company
  if any candidate's description similarity >= repost_similarity:
      store the new row with repost_of = that job
      record a dismissed decision on it ("repost of <id>")
  if the embedder is unavailable: store the row as it is (fail open: the job stays visible)

name lookup:
  slugs = slug_candidates(name)           # existing, with better name cleaning
  for each slug, probe all eight platforms   # existing resolve_company
  show the best hit -> user confirms -> board added to the crawl
  nothing found -> "Search the web?" -> user says yes -> discovery agent for this one company
```

## Testing

| Layer | What it proves |
|---|---|
| Unit | The rank formula with and without a CV; the repost check (same company and close text hides it; different company, same text does not); the note is empty with no decisions, bounded, and counts reasons. |
| Unit | Name cleaning: umlauts, "&", ".com", legal suffixes. |
| Contract | `job_decisions.reason` and `jobs.repost_of` round-trip and old rows still load; the digest query never returns a dismissed job. |
| E2E | Dismiss a job, ingest a reworded repost with a new id and URL: it is hidden. A run with no ✓ or ✕ sends no note. |
| E2E | The lookup API finds a board from a stubbed fetcher, adds it only after confirm, and 404s a wrong guess without changing anything. |
| Frontend | ✕ shows the reason choice and works without one; the add-source box shows a hit and the confirm step; the job list filters by company. |
| Manual | On a real account: dismiss a job, run discover again, and check it stays hidden. Look up a known Ashby company in the UI and confirm it crawls. |

## Non-goals

Hiding jobs from patterns the user never dismissed (company or title rules learned automatically), expiring dismissals, matching reposts across different companies, new job platforms beyond the eight, and per-user ranking weights.

## Delivery

One spec, six small branches, merged in this order:

1. `fix/digest-dismissed-leak`
2. `fix/score-floor-45`
3. `feat/repost-dismissal-memory`
4. `feat/feedback-preference-summary`
5. `feat/cv-similarity-ranking`
6. `feat/company-name-lookup`
