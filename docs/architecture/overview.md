# Overview: style and layers

Why the system is hexagonal and how the layers depend on each other.

Back to the [architecture index](../architecture.md).

## Style: hexagonal, four core ports

Two forces drive the shape: the model server, the job board and the store must each be
swappable, and a run must work with a local model, a cloud model or a fake in tests.
Both are "same contract, different implementation" — that is a port.

A port exists only where a second implementation is real: a fake for tests, a second
vendor, a second backend. Everything else is a plain injected object.

| Port | Contract | Adapters |
|---|---|---|
| `JobSource` | `fetch(target) -> Iterable[RawJob]` | ATS boards: Greenhouse, Lever, Ashby, Workable, Recruitee, SmartRecruiters, Personio, Teamtailor (`adapters/sources/ats.py`, per-host `HostThrottle`) |
| `StateStore` | jobs, scores, runs, cooldowns | SQLite file (`adapters/store/sqlite_store.py`) |
| `LLMClient` | `complete(prompt, schema, system, validator) -> LLMResponse` | `LiteLLMClient` (Ollama, vLLM, OpenRouter), `RedactingLLMClient` in front of it, `FakeLLM` (tests) |
| `Notifier` | `deliver(title, body)` | `MarkdownNotifier` (digest file); the review UI reads the store directly |

Later features add narrow ports beside these, each with a fake or a second backend:
`PreferencesStore`, `CvStore`, `IntentStore`, `SampleRoleStore`, `SearchProfileStore`,
`ApplicationLog` (Notion Applications), `PiiDetector`, `Recorder`, `Embedder`,
`JobVectorStore`, `SourceCatalog`, `SearchProvider`, `CompanyListProvider`,
`CompanyListStore`. All are in `ports.py`.

Not ports: config, secrets, `RunLimits` / `CostMeter`. Plain objects, constructor-injected.

## Layers

```
drivers        CLI (Typer, __main__.py, cli_sources.py) · FastAPI (adapters/api/) · Next.js UI
use cases      usecases/: discover, score, digest, discover_sources, curate, assistant, ...
domain         pure functions + dataclasses, zero I/O, stdlib only
ports          ports.py: the protocols above
adapters       one directory per concern: sources, store, llm, pii, embed, search, resume, notify
agents         agents/: model-driven steps, called from use cases
```

Imports point inward only. `domain/` imports the standard library only; `usecases/`
import `domain/` and `ports.py`, never an adapter. `usecases/pipeline.py` is the
composition root: it wires adapters to use cases for both the CLI and the browser.

**Domain** holds the models and rules: `RawJob`, `JobPosting`, `SourceTarget`,
`Preferences`, `Cv`, `Score`, the lifecycle of a decision, `OwnerIdentity`, and the pure
functions `normalize`, `canonical_url`, `fingerprint`, `apply_hard_filters`, the
per-company cap in `diversity.py`. No mocks are needed to test it.

**Use cases** are linear and readable — the pipeline stays visible, not hidden behind
events. Each is runnable from the CLI (`discover`, `score`, `digest`, `run`, `doctor`,
`ui`, `list`, `compare`, `export-top`, and the `sources` group). `career-copilot ui`
serves the API and the exported frontend on one port.

**No profile identity anywhere in code or schema.** The CV and the preferences are data
in the store (`cv_versions`, `preferences`); `config.yaml` holds no personal data.
