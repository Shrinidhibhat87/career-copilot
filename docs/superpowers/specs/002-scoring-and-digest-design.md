# Scoring and Digest — Design

> Hard filters drop the obvious misses. A cheap triage pass orders the survivors. An expensive match pass scores the top few as four named parts out of 100. A Markdown digest ranks the result. Core rule: model calls are the only spend, so they are budgeted, never repeated for unchanged work, and a hard-filter reject is never rescued by a score. Builds on `001-foundation`; preferences come from `005-preferences`, run tracking from `006-runs-and-observability`, ranking by embeddings from `007-review-and-recommendations`.

## Decisions

| Topic | Decision |
|---|---|
| Order of work | Discover, hard filter, triage every survivor, match the top few, digest. |
| Hard filters | Cheap rules in `domain/filters.py` run before any model call. A reject is final. Unclear cases go to `manual_review`, never to a silent drop. |
| Role gate | A title with an `excluded_role_terms` hit is rejected. A title matching no `title_terms` entry is parked for review. An empty `title_terms` turns the gate off. |
| Stack rules | Zero `core_stack_terms` hits in title and description rejects ("no profile-stack signal"). More distinct `excluded_stack_terms` hits than core hits parks for review. One core hit protects a posting from rejection. |
| Matching terms | Terms match whole words, any case, phrases with flexible spaces. Hits count distinct terms, not repeats. An empty `core_stack_terms` turns both stack rules off. |
| Seniority and language | Rules follow the saved seniority bands and German level (see `005-preferences`). |
| Load modes | Initial mode when no `match` score exists, else incremental. Triage rows do not count, because they appear mid-run. |
| Targets | `initial_target` 50, `incremental_target` 6. `--initial` forces the first, `--budget N` overrides both. |
| Counting a match | A match counts toward the target only at or above `scoring.min_match_score` (default 45). Weaker matches are still stored. |
| Call cap | Match calls stop after 3 times the target (2 times on the embedding path). A short run reports the shortfall and does not pad. |
| Triage | Runs on every survivor. Returns relevance 0 to 100 and one sentence, from the first 1200 characters of the posting. |
| Candidate order | The `ranker` setting picks `embedding` (default) or `llm_triage`. The embedding path falls back to triage when it cannot rank. |
| Company cap | `max_per_company` (default 2) applies at three places: match selection, digest, calibration export. Storage is never capped. |
| Discovery target | Discovery stops draining boards once `stop_after` new qualified jobs exist (`initial_target` while the jobs table is empty). Each board gives at most `max_jobs_per_target`. |
| Match output | Four parts: Skills 40, Seniority and role 25, Domain 20, Preferences and location 15. Each has points and one sentence. |
| Total | Code adds the parts. The model returns no total and no verdict. |
| Verdict | 70 and above strong, 45 and above possible, else weak (`ScoreVerdict.from_score`). |
| Bounds | A part outside its range is a schema failure. The client retries once with the error. |
| Evidence | The resume is sent with line IDs `[L12]`. `evidence_refs` holds IDs only. An empty or unknown ID is `MissingEvidenceError`, never a low score. |
| Failed job | After the retry the job is skipped and counted as failed. The run continues. |
| Rubric version | `RUBRIC_VERSION` is 2. A bump makes every older score stale. |
| Old scores | Version 1 scores stay visible with `parts` null and the note "Scored before breakdowns existed". Lists read the highest rubric version per job. |
| Rescoring | A job needs scoring unless a match row exists with the same `content_hash`, `rubric_version`, `model` and `preferences_hash`. Dismissed and applied jobs never need scoring. A new CV does not. |
| Donut | The card shows the total in a donut with four arcs. Clicking an arc or legend row toggles that part's sentence. Triage keeps one total and no donut. |
| Money | `max_eur_per_run` is a hard ceiling. Each call reserves its worst case first. A breach aborts, records the reason, exits non-zero. |
| Digest | Query plus template, no model call, same inputs give the same bytes. Only scores under the current model, rubric and preferences hash appear. |
| Silent failures | Failed sources and unresolved watchlist companies are listed, so a broken scrape never reads as "no jobs today". |
| Calibration | A seeded, company-capped, band-stratified export feeds a human-labelled opt-in test. Labels are human, never model-written. |

## Shapes

```python
# domain/scoring.py
RUBRIC_VERSION = 2
PART_MAX = {"skills": 40, "seniority": 25, "domain": 20, "preferences": 15}   # sums to 100

@dataclass(frozen=True, slots=True)
class Score:                      # one result plus the stamp that decides if it is still valid
    job_id: int; stage: ScoreStage; score: int; verdict: ScoreVerdict
    rubric_version: int; model: str; prompt_hash: str; content_hash: str
    reasons: tuple[str, ...]; gaps: tuple[str, ...]; warnings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    preferences_version: int; preferences_hash: str   # hash joins the identity index
    cv_sha256: str | None                              # provenance only
    parts: dict[str, dict] | None                      # {"skills": {"points": 30, "reason": "..."}}

# agents/match.py: model reply (no score, no verdict)
# {skills, seniority, domain, preferences: {points: int, reason: str},
#  reasons: [str], gaps: [str], warnings: [str], evidence_refs: ["L12"]}   evidence min 1
```

```yaml
# config.yaml (scoring: and filters:)
stop_after: 6
initial_target: 50
max_jobs_per_target: 100
scoring:
  initial_target: 50
  incremental_target: 6
  min_match_score: 45             # default in ScoringSpec
  triage_max_output_tokens: 256
  match_max_output_tokens: 1024
  triage_reserve_eur: 0.02        # per-call worst case, includes the one retry
  match_reserve_eur: 0.20
  max_eur_per_run: 1.0
  rubric_version: 2
  ranker: embedding               # or llm_triage
  digest_path: data/digest.md
filters:                          # seed for preferences, see 005-preferences
  excluded_role_terms: [...]  title_terms: [...]  core_stack_terms: [...]
  excluded_stack_terms: [...]  max_per_company: 2
```

Storage (`adapters/store/migrations.py`): migration 2 creates `scores` with the unique index `idx_scores_identity` and adds `runs.llm_calls` and `runs.cost_eur`. Migration 3 adds `preferences_hash` to that index. Migration 21 adds `scores.parts` (JSON, null for version 1 and triage).

## Flow

```
score_run(budget):
    candidates = jobs_needing_score(match_model, rubric_version, preferences_hash)
    if dry_run: report candidate count and reserved cost; return          # no model call
    ordered = ranker(candidates)                                          # embedding path
    if ordered is None:                                                   # no references or embedder broken
        for job in candidates:
            reserve(triage_reserve_eur)                                   # over ceiling -> abort
            try: triage(job) -> record_score(stage=triage)
            except SchemaError: skip, count failed
            except LLMCallError: abort, record reason
        ordered = sort(triaged, by -relevance, id)
    pool = cap_per_company(ordered, max_per_company)
    selected = pool[: target * call_factor]
    for job in selected:
        if above_bar >= target: break
        reserve(match_reserve_eur)
        try: out = match(job)
        except SchemaError: skip, count failed                            # bounds, missing evidence
        except LLMCallError: abort
        score = sum(part.points); verdict = from_score(score)
        record_score(score, verdict, parts, rubric_version, preferences_hash, cv_sha256)
        clear_needs_rescore(job); if score >= min_match_score: above_bar += 1
    record_run_cost(calls, spent)                                         # shortfall is reported

digest(limit=20):
    scored = match_scores(model, rubric_version, preferences_hash)        # highest rubric per job
    show rank(scored) capped per company: score, why, gaps, warnings, evidence
    plus Manual review, Statistics, Source failures, Watchlist coverage
    if no scored jobs: "No scored jobs yet. Run `career-copilot score`."

card(score): parts is None -> total + legacy note; else donut + legend, click toggles part sentence
```

CLI: `career-copilot score [--initial] [--budget N] [--dry-run]`, `digest [--out PATH] [--limit N]`, `run [--initial]` (discover, score, digest). The CV comes from the stored CV (`010-persistence-and-lifecycle`).

Calibration export (`scripts/export_calibration_set.py`, run after `score`): bucket triage-scored passing jobs into high (70 and up), mid (40 to 69), low; round-robin across companies; cap per company across the whole file; take about a third per band, borrowing from a neighbour band when short; `--seed` (default 0) makes the CSV repeatable. With no triage score it exits non-zero and names `career-copilot score`.

## Testing

| Layer | What it proves |
|---|---|
| Unit `test_scoring.py` | `rank` orders by score, then recency, then id, and is stable. Verdict bands are 70 and 45. |
| Unit `test_filters.py`, `test_filters_preferences.py` | Each filter rule fires; Python plus TypeScript passes; one core hit blocks rejection; repeated terms count once; empty core list fails open. |
| Golden `test_filter_corpus.py` | Real postings keep their recorded outcomes and reasons; matching stays case-insensitive. |
| Unit `test_diversity.py`, `test_calibration_export.py` | Company cap keeps order and is stable; same database and seed give the same CSV, capped, all bands present. |
| Contract `test_state_store.py`, `test_score_parts_store.py` | Store identity and rescoring rules. Migration 21 keeps rows with null `parts`. Version 1 jobs still list; the highest version wins; `needs scoring` is true for version 1 and false for version 2. |
| E2E `test_score_and_digest.py` with `FakeLLM` | Triage runs on all survivors, match on the budget only, capped per company. A second run scores nothing; a changed posting rescores. Missing evidence parks the job. The euro ceiling aborts. Weak matches do not count; shortfall is reported. Stored parts sum to the stored total; triage has none. |
| E2E `test_discover_run.py` | A target stops free boards too; cooldowns and `max_jobs_per_target` hold; failed fetches are recorded. |
| Golden `test_digest.py` | Digest bytes are stable and no company exceeds the cap. |
| Vitest `review-card.test.tsx` | Donut shows the total; clicking a part shows its sentence; version 1 shows the note and no donut. |
| Calibration (manual, `-m calibration`, never in CI) | Needs a live model and `tests/calibration/labels.csv`. Skips under 10 labels. Match scores should separate the human `strong`/`ok`/`no` labels. |

CI runs everything except calibration, offline. Correctness is judged by the sum invariant, the bounds check, the idempotent rerun (zero duplicates, zero rescores, identical digest) and the human labels.

## Non-goals

Per-user part weights, parts for triage, rescoring all old jobs at once, and any change to how boards are fetched.
