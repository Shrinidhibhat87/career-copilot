# Preferences — Design

> One saved `Preferences` model is the source of truth for the hard filters and for the `PREFERENCES` block in every prompt. It is edited on `/preferences`, versioned in SQLite, and stamped on every score. Core rule: a value is stored once, in one place, and a change re-filters stored jobs and re-opens only the scores it could change. Builds on `001-foundation` and `002-scoring-and-digest`; resume prefill comes from `003-frontend-onboarding`, redaction from `004-pii-gateway`.

## Decisions

| Topic | Decision |
|---|---|
| Source of truth | `domain/preferences.py::Preferences` feeds both `to_filter_config()` and `to_prompt_block()`. Nothing in a run reads `config.yaml` `filters:` once a version is saved. |
| Bootstrap | With no saved row, preferences are built in memory from `config.yaml` `filters:` and `profile/preferences.md` (read into `notes`) at `version = 0`. They are never written. Saves go to the `preferences` table (migration 3, JSON payload, first save is version 1) behind `PreferencesStore`. |
| Terms, not labels | Term lists hold lowercase match terms. Display labels (`LLM / RAG`) are chips only. `notes` goes into the prompt as typed. `role_intent` (max 600 characters) is the user's own words. |
| Search profile | `search_profile` (the profiler's reading of intent, sample roles and CV) is attached per run, never saved, and replaces `role_intent` in the prompt. |
| Role gate | `title_terms` is a title matcher for the positive role gate. It is not in the prompt. A row saved before it existed gets the default list. A list saved empty means no gate. |
| Seniority | Bands `junior`, `mid`, `senior`, `staff`, `lead`; default `mid`, `senior`. A title is classed (leadership, staff, junior, senior, else mid) and rejected if its band is unselected. |
| Language rule | Only German is matched. A demand above the user's German level rejects. "German is a plus" warns. A demand for an unlisted language goes to `manual_review`. Defaults `en C2`, `de B1` reproduce the old behaviour. |
| Limits | At most 5 `role_families`. At least one city and one band. CEFR levels only, one row per language. `batch_size`, `max_per_company` at least 1. |
| Score identity | `prompt_hash()` hashes the rendered prompt block. A field affects it only by being rendered. `max_per_company`, `batch_size`, `version`, `updated_at` and `title_terms` do not. |
| Re-filter on save | Saving re-runs the hard filters over all active jobs in the same transaction. No model call, no score row. The next score run re-opens exactly the jobs with no match under the new hash; the digest hides older scores until then. |
| Preview | `POST /api/preferences/preview` runs the same re-filter as a save over a draft and writes nothing (`plan_refilter`; the save is `plan_refilter` plus its writes, so the two cannot disagree). The form asks for it, after a short pause, only once a field a filter reads has changed. It shows the stored jobs that would move and, per excluded role term, how many stored titles hold the word and how many of those would be newly hidden. A term matches whole words anywhere in the title, so "manager" also hits "Senior Manager". |
| Extraction | An opt-in call turns notes into `FieldChange` proposals. Bad fields or values are dropped (same `validate` as a save). The user accepts one at a time; nothing persists until Save. It uses the `triage` client inside `RedactingLLMClient`, so notes are redacted first. |
| Resume prefill | While `version = 0`, `GET` merges detected roles, skills and languages and badges them. A level is never invented. A saved version is never overridden. |

## Shapes

```python
# domain/preferences.py
CefrLevel = Literal["A1","A2","B1","B2","C1","C2","native"]   # SeniorityBand: junior..lead
@dataclass(frozen=True, slots=True)
class Preferences:
    role_intent: str            # own words, max 600 chars, prompt only
    search_profile: str         # per run, not saved with the form
    role_families: tuple[str, ...]      # max 5, prompt only
    title_terms: tuple[str, ...]        # title matcher, not in the prompt
    core_skills: tuple[str, ...]        # -> FilterConfig.core_stack_terms
    excluded_stack: tuple[str, ...]; excluded_role_terms: tuple[str, ...]
    seniority: tuple[SeniorityBand, ...]
    allowed_cities: tuple[str, ...]; preferred_cities: tuple[str, ...]; flagged_cities: tuple[str, ...]
    allow_remote: bool; require_permanent: bool; languages: tuple[LanguageSkill, ...]   # name + level held
    max_per_company: int; batch_size: int   # operational, outside the hash; batch_size feeds the incremental target
    notes: str
    version: int; updated_at: datetime | None   # version 0 = in-memory bootstrap

@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str; action: Literal["add","remove","set"]; value: str; rationale: str  # value: "name:LEVEL", "true", or digits
```

```sql
-- migration 3 (adapters/store/migrations.py)
CREATE TABLE preferences (version INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL,
                          prompt_hash TEXT NOT NULL, created_at TEXT NOT NULL);
ALTER TABLE scores ADD COLUMN preferences_version INTEGER NOT NULL DEFAULT 0;  -- what I held then
ALTER TABLE scores ADD COLUMN preferences_hash TEXT NOT NULL DEFAULT '';       -- would it score the same
-- idx_scores_identity gains preferences_hash; '' is never a real hash, so old scores read stale
```

```text
GET  /api/preferences          -> Preferences with {term, label} pairs, resume badges while version 0
PUT  /api/preferences          -> { preferences, refiltered: {examined, changed, transitions, nowPassing}, reopenedForScoring }
                                  422 { errors: { field: message } }
POST /api/preferences/preview  -> { examined, changed, transitions, nowPassing, hidden,
                                    excludedRoleTerms: { term: { matches, newlyHidden } } }
                                  422 { errors } for an invalid draft · writes nothing
POST /api/preferences/extract  -> { changes: [{field, action, value, label, rationale}], costEur }
                                  409 no backend or key · 422 bad draft · 502 provider failure
```

## Flow

```
load(store, seed): return store.load_current() or seed            # seed = bootstrap, version 0

save(form):
    prefs = read_form(form); validate(prefs)   # wrong type or invalid field -> 422, nothing written
    in one transaction:                # any failure rolls back both
        saved = store.save(prefs)      # next version, prompt_hash stored
        report = apply_preferences(saved)   # re-run filters on all active jobs, update outcomes
    return saved, report.transitions, report.reopened_for_scoring

extract(notes, draft?):
    client = redacting(triage_role_client)        # BackendUnavailable -> 409
    current = validated draft, else loaded        # invalid draft -> 422
    changes = model(notes) kept only if is_applicable   # LLMCallError -> 502; writes nothing

score, digest: prefs = load(); filters from to_filter_config(); prompt from to_prompt_block(); stamp prompt_hash()
```

## Testing

| Layer | What it proves |
|---|---|
| Unit `test_preferences.py` | Validation per field. Operational fields leave `prompt_hash` equal; a prompt field changes it. Payload round trip and old-row defaults. |
| Golden `test_filter_corpus.py` | Bootstrapped preferences give the same outcome, reasons and warnings as `config.yaml` for every recorded posting. This, not config equality, judges that behaviour did not change. |
| Unit `test_filters_preferences.py` | A C1 German demand rejects at `de B1` and passes at `de C1`. "Plus" warns at any level. An unlisted language parks. Band rules fire only while their band is unselected. |
| Contract `test_preferences_store.py` | Versioning and round trip. Migration 3 upgrades an existing database. `jobs_needing_score`, `match_scores` and `digest_stats` ignore a score under another hash. |
| Contract `test_preferences_api.py` | Each route and status. Bootstrap and prefill badges. Save survives restart. A failed save or re-filter rolls back. Extraction writes nothing, uses the unsaved draft, drops hostile proposals (also `test_extract_preferences.py`). Re-filter moves outcomes with no score row (`test_apply_preferences.py`). |
| Vitest `preferences.test.tsx` (`src/frontend/tests`) | Chips, the five-role limit, band and level edits, accept or reject proposals, save counts, 422 marks. |
| Manual | Upload the CV, extract a note, accept one change, save. `career-copilot list` and `score --dry-run` agree with the counts shown. |

CI runs all but the manual row. Correctness is judged by the golden corpus and the hash and re-open checks.

## Non-goals

Languages other than German, multi-user preferences, and applying a model proposal without the user accepting it.
