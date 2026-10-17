# Operations

Model hosting, testing, runtime, security and failure handling.

Back to the [architecture index](../architecture.md).

## Model deployment

Every model role (`triage`, `match`, `profiler`, `curator`, `assistant`, `discovery`)
is bound to a backend in `models:` in `config.yaml`. One `LiteLLMClient` serves every
backend kind; `adapters/llm/backends.py` is the only file that knows a litellm prefix
or a health-probe URL. A role is independent, so triage can be local and match remote.

| Where | Runtime | Profile / config | Models |
|---|---|---|---|
| macOS, Apple Silicon | Ollama, native (Docker cannot reach Metal) | `deploy/profiles/mac-ollama.yaml` | `qwen3:4b-instruct` for triage and agents, `qwen3:14b` for match |
| Windows (WSL2) or Linux with an NVIDIA GPU | vLLM in Docker, `deploy/vllm/docker-compose.yml`, host port 8001 | `deploy/profiles/nvidia-vllm-awq.yaml` | 4-bit AWQ Qwen3-4B-Instruct as `qwen3-4b-instruct`, `--dtype half` on Turing cards |
| macOS, native MLX | vLLM-Metal (`kind: vllm`, a different `base_url`) | `models.backends.vllm_metal` in `config.yaml` | any model it serves |
| Remote | OpenRouter (`kind: openrouter`), needs `OPENROUTER_API_KEY` in `.env` | `models.roles.<role>.backend: openrouter`, or the legacy `scoring.triage_model` / `match_model` | e.g. Gemini Flash for triage, Gemini Pro for match |

- A profile holds only a `models:` block and replaces the same-named backends and
  roles. Choice order: env `CAREER_COPILOT_PROFILE`, then `models.profile`, then
  auto-detection by hardware (`apple_silicon`, `nvidia`, `other`). `none` turns
  profiles off and uses `config.yaml` as written. An unknown name stops start-up.
- Local backends use the dummy key `local` and report cost 0. Because euros do not
  bind a local run, `RunLimits` also caps steps and seconds.
- Redaction stays on for local and remote roles alike (see Security and PII).
- `career-copilot doctor` prints the chosen profile and why, whether each role's
  backend is reachable, and whether its model is present.

## Testing

| Level | Scope | Rule |
|---|---|---|
| Unit | domain functions | no mocks, no I/O, fast |
| Contract | stores and API on a real temp SQLite | dedupe, upsert outcomes, cooldowns, migrations |
| Golden | ATS parsers, filter corpus, digest text | recorded fixtures, no live network |
| E2E | use cases on `FakeLLM`, fake search, fake embedder, temp SQLite | assert digest content, then rerun and assert zero duplicates and zero rescores |
| Calibration | match scores on a live model | hand-labelled set; manual only, `-m calibration` |

CI (`.github/workflows/ci.yml`) runs `ruff check`, `ruff format --check`, strict `mypy`
and `pytest`, with no network or model calls. The E2E rerun-idempotency assertion is the
single most important test in the suite.

## Runtime

Local only. One idempotent command, `career-copilot run` (or `ui`), against a SQLite
file; secrets come from `.env`. The model side is the only part that varies per machine,
and it is configuration (see Model deployment). A hosted or scheduled runner is not
built; the port boundaries leave the slot open.

## Security and PII

The CV, preferences and database stay out of git (`.gitignore`). A Presidio-based
redaction gateway (`RedactingLLMClient`, `adapters/pii/`) sits in front of every
provider call, local or remote: the owner's name, email, phone, address and URLs become
placeholders on the way out and are restored on the way back. It fails closed: if text
cannot be scanned, the provider is not called. The owner's identity is derived from the
stored CV. Employers, schools and dates stay in the prompt, so redaction is
pseudonymous, not anonymous. See [`PII_HANDLING.md`](../../PII_HANDLING.md).

## Failure modes

| Failure | Response |
|---|---|
| Source down or markup changed | Cooldown, run continues, recorded in `source_runs` |
| Host rate-limits (429) | Per-host throttle and retry; a long `Retry-After` blocks that host for the run |
| LLM returns invalid JSON | Retry once with the schema error; then the job is skipped and counted failed |
| Provider failure | Classified as a `FailureCategory` (transport, credit, rate limit, provider, schema, evidence) with a user-facing message; scores so far are kept |
| PII scan cannot run | Provider not called, run aborts with the spend recorded |
| Budget exceeded | Abort run loudly, record reason, no partial silent spend |
| Duplicate across sources | Fingerprint match, `last_seen_at` updated, no rescore |
| Ambiguous eligibility wording | `manual_review`, surfaced in the review screen |
| Run left in `running` | Blocks the next run with "a run is already active" |
