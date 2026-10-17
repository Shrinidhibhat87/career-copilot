# Foundation — Design

> `career-copilot` finds job postings on public ATS boards, drops the ones that cannot fit, scores the rest with models and renders a ranked digest. Core rule: the domain knows nothing about infrastructure; everything outside it plugs in through a port in `ports.py`, and anything uncertain is parked for a human, never silently thrown away. Every later spec (`002-scoring-and-digest` to `011-agentic-recommendations`) builds on this one.

## Decisions

| Topic | Decision |
|---|---|
| Style | Hexagonal. The app core calls ports. Adapters implement them. A model server, a job board or a store can be swapped without touching the core. |
| Port rule | A port exists only where a second implementation is real: a fake for tests, a second vendor, a second backend. |
| Core ports | `JobSource`, `StateStore`, `LLMClient`, `Notifier`. Later specs add narrow ports beside them (`PreferencesStore`, `CvStore`, `SearchProvider`, `PiiDetector`, `Recorder` and others). |
| Layers | `domain/` (rules and models), `ports.py` (contracts), `usecases/` (orchestration), `adapters/` (infrastructure, including `adapters/api/`), `agents/` (model-driven steps), `__main__.py` (Typer CLI). |
| Dependency rule | Imports point inward only. `domain/` imports the standard library only. `usecases/` import `domain/` and `ports.py`, never an adapter. Adapters import ports and domain. Shared errors (`LLMCallError`, `SchemaError`) live in `ports.py` so use cases can catch them without importing an adapter. |
| Composition root | `usecases/pipeline.py` wires adapters to use cases for both the CLI and the browser. |
| Pipeline | discover, hard filters, dedupe, score (triage then match), digest. Discovery and filtering use no model. |
| Collection ladder | Targets are sorted by `tier`, then least recently visited first (never visited ahead of all; `cooldowns.last_scraped_at`). Each is fetched unless it is cooling down. The run stops early once `stop_after` new qualifying jobs are found (`initial_target` while the store has no jobs). One target contributes at most `max_jobs_per_target` postings. |
| Dead source | A failing fetch is recorded in `source_runs`, the target gets a longer cooldown and the run goes on. A failure is never counted as zero jobs. |
| Dedupe | `StateStore.upsert_job` looks for an existing job by source id, then `canonical_url`, then `fingerprint` (company, title, location). A match updates the row and logs the change in `job_versions`. A changed `content_hash` flags the job for rescoring. Nothing is deleted on a repeat sighting. |
| Hard filters | `apply_hard_filters` is rule-based: location, language, employment type, seniority, role family, stack, eligibility. Result is `pass`, `manual_review` or `reject`. A reject always carries reasons. |
| Failure principle | Missing or unclear data (for example a title that matches no role term) gives `manual_review`, not `reject`. A model failure surfaces as a `FailureCategory` and a user-facing message. Nothing is guessed. |
| Inactive jobs | A job unseen for `inactive_after_days` becomes `inactive`. Rows are never deleted by the crawl. |
| Data store | One SQLite file (`db_path`, default `data/jobs.db`). Schema changes are numbered migrations in `adapters/store/migrations.py`, applied by `migrate()` and recorded in `schema_migrations`. Migrations are append-only. The current top version is 21. |
| Config | `config.yaml`, parsed by `config.py` into pydantic specs. Targets, filters, models, budgets and digest path are data, not code. A new company board is a new `targets` line. |
| Secrets | API keys come from `.env`. `config.yaml` holds no personal data. |
| Models | `models.backends` and `models.roles` choose which model serves `triage`, `match`, `assistant` and `discovery`. `LLMClient.complete` returns schema-checked data or raises. |
| Entry points | CLI commands in `__main__.py` (`discover`, `score`, `digest`, `run`, `doctor`, `ui`, `list`, `compare`, `export-top`) plus the `sources` group in `adapters/cli_sources.py`. `ui` serves the API and the built frontend on one port. |

## Shapes

```python
# ports.py: the four core contracts
class JobSource(Protocol):
    name: str
    def fetch(self, target: SourceTarget) -> Iterable[RawJob]: ...   # empty is valid, never raise on empty

class LLMClient(Protocol):
    def complete(self, prompt, schema, system=None, validator=None) -> LLMResponse: ...  # validated or raises LLMCallError

class Notifier(Protocol):
    def deliver(self, title: str, body: str) -> None: ...            # MarkdownNotifier writes scoring.digest_path

class StateStore(Protocol):   # canonical state: jobs, scores, runs, cooldowns
    def upsert_job(self, posting, verdict, now) -> tuple[int, UpsertOutcome]: ...
    def record_score(self, score, now, cost_eur=None) -> int: ...
    def is_cooled_down(self, source, identifier, now) -> bool: ...
```

```python
# domain/models.py
class FilterOutcome(StrEnum): PASS; MANUAL_REVIEW; REJECT   # hard-filter verdict
class UpsertOutcome(StrEnum): INSERTED; UNCHANGED; CHANGED  # tells callers if work is needed
class RawJob: ...        # what a source returns
class JobPosting: ...    # normalized, with fingerprint and content_hash
class SourceTarget: ...  # source, identifier, tier, cooldown_hours
```

```yaml
# config.yaml (shape)
db_path: data/jobs.db          # the one store
stop_after: 6                  # ladder stop; initial_target while the store is empty
max_jobs_per_target: 100       # cap per board per run
inactive_after_days: 21        # unseen jobs go inactive
models: {backends: {...}, roles: {triage: ..., match: ...}}
scoring: {initial_target, incremental_target, max_eur_per_run, rubric_version, digest_path}
filters: {...}                 # bootstrap seed for preferences (see 005-preferences)
targets: [{source: greenhouse, identifier: n26, tier: 0, cooldown_hours: 72}]
```

## Flow

```
run():
    store = migrate(db)                              # pending numbered migrations only
    discover:
        for target in sorted(targets, by tier, then last visit):   # never visited first
            if stop_after met:        skip "target met"
            if no adapter:            skip "no adapter"
            if cooling down:          skip "cooldown"
            try:   raw = source.fetch(target)[:max_jobs_per_target]
            except: record failed source_run, set longer cooldown, continue
            for job in raw:
                posting = to_posting(job)             # normalize, fingerprint, content_hash
                verdict = apply_hard_filters(posting) # pass | manual_review | reject
                store.upsert_job(posting, verdict)    # dedupe ladder; rejects are stored, not dropped
    score:                                           # see 002-scoring-and-digest
        candidates = jobs that passed and need a score
        triage then match through LLMClient
        except LLMCallError: finish the run with the category message, keep what was scored
    digest:
        Notifier.deliver(render(top scores))
    manual_review jobs stay visible for a human decision
```

## Testing

| Layer | What it proves |
|---|---|
| Unit (`tests/unit`) | Domain rules without I/O: normalization, filters, config parsing, budgets, scoring maths. |
| Golden (`tests/golden`) | Fixed inputs give fixed outputs: ATS parsers against `tests/fixtures`, the filter corpus, the digest text. A diff means behaviour changed. |
| Contract (`tests/contract`) | Each store and API behaves as its port says, on a real temp SQLite: dedupe ladder, upsert outcomes, cooldowns, migrations. |
| E2E (`tests/e2e`) | Use cases run end to end on fakes (`FakeLLM`, fake search, fake embedder) and fixtures: discovery, scoring and digest, CLI commands. |
| Calibration (`tests/calibration`) | Scores against a labelled set on a live model. Manual only: marker `calibration`, run with `-m calibration`, excluded by default. |

- CI (`.github/workflows/ci.yml`) runs `ruff check .`, `ruff format --check src tests`, `mypy` (strict, on `src` and `tests`) and `pytest`. The suite makes no network or model calls.
- Correctness is judged by the fixtures and golden files, and by explicit assertions that failures are recorded (a dead source yields a failed `source_run`, not zero jobs).

## Rules for later designs

- A new capability is a new port or a new adapter, not a change to the core.
- Use cases orchestrate. Rules live in `domain/`, I/O lives in adapters.
- No infrastructure (SQL, HTTP, model SDKs, file paths) in `domain/`.
- Migrations are numbered and append-only. A shipped migration is never edited.
- Behaviour is config-driven. Thresholds, budgets and lists go in `config.yaml`, not in code.
- Unclear data is parked for review, never rejected silently.
- Every spec states its CI checks and what stays manual.

| Spec | Extends |
|---|---|
| 002-scoring-and-digest | `usecases/score.py`, `agents/triage.py`, `agents/match.py`, `domain/scoring.py`, `domain/diversity.py`: triage and match agents, relevance screening, per-company caps, score parts. `LLMClient`, `StateStore`, `Notifier`. |
| 003-frontend-onboarding | `adapters/api/` (first FastAPI boundary), `src/frontend` Next.js UI, sign-in, resume upload, landing. |
| 004-pii-gateway | `adapters/pii/`, `PiiDetector`, `RedactingLLMClient`: redaction in front of every provider call. |
| 005-preferences | `PreferencesStore`, `domain/preferences.py`: the profile as source of truth over the `filters:` seed. |
| 006-runs-and-observability | `domain/runs.py`, `runs` ledger, `Recorder`, `adapters/observability.py`, settings: pipeline in the browser, run history, metrics. |
| 007-review-and-recommendations | `adapters/api/review.py`, `Embedder`, `JobVectorStore`: retrieval precision, the review and recommendations screens. |
| 008-local-model-runtime | `adapters/llm/backends.py`, `adapters/profiles.py`, `models:` config: Ollama and vLLM, model profiles. |
| 009-source-discovery | `agents/discovery/`, `SourceCatalog`, `SearchProvider`, `CompanyListProvider`, `adapters/sources/`: discovery agent, company-list import, more ATS vendors. |
| 010-persistence-and-lifecycle | `CvStore`, `job_decisions`, recycle bin, `ApplicationLog`: versioned CV, decisions, applied jobs and Notion. |
| 011-agentic-recommendations | `IntentStore`, `SampleRoleStore`, `SearchProfileStore`, `agents/curator/`, `agents/assistant/`: intent, curator, assistant chat. |

## Non-goals

No scraping behind logins, no auto-applying, no multi-user hosting, and no deletion of crawled jobs by the crawl itself.
