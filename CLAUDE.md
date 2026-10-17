# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Purpose

`career-copilot` is a personal job-search agent: it discovers job postings from ATS boards, hard-filters them, scores survivors with LLMs, and renders a ranked digest. Hexagonal architecture: ports insulate the app from infrastructure, so a local model server, a cloud provider, or a different job board are swap-in adapters, not rewrites.

Read `docs/architecture.md` for the full design before making structural changes.

## Architecture

- **Ports** (`src/career_copilot/ports.py`): `JobSource`, `StateStore`, `LLMClient`, `Notifier`.
- **Adapters** implement each port: `adapters/sources/*` (Greenhouse etc.), `adapters/store/sqlite_store.py`, `adapters/llm/litellm_client.py` (+ `backends.py` for health probes), notifiers.
- **Usecases** (`usecases/pipeline.py`, `usecases/discover_sources.py`) orchestrate: discover → score (triage, then match) → digest.
- **Agents** (`agents/discovery/agent.py`) use `pydantic_ai.Agent` directly, not the `LLMClient` port, with tool-calling (`web_search`, `probe_ats`, `sample_jobs`, `propose_source`) to discover ATS boards, budget-gated by `discovery:` config.
- **CLI** (`__main__.py`, Typer; the `sources` group is in `adapters/cli_sources.py`): `discover`, `score`, `digest`, `run`, `doctor`, `ui`, `sources`, `list`.
- **Frontend** (`src/frontend`, Next.js, static export, `output: export`): built once via `npm run build`, then served by FastAPI from one origin via `adapters/api/static_ui.py::mount_ui`. The build script is pinned to `next build --webpack`: Next 16.3.2's Turbopack has a `next/font/google` resolution bug that webpack avoids.

## Model configuration (`config.yaml`)

- `models.backends` / `models.roles` pick which model serves `triage`, `match`, and `discovery`, and where it lives (`kind: ollama | vllm | openrouter`).
- Local backends (`ollama`, `vllm`) use a dummy `api_key="local"`; `openrouter` needs `OPENROUTER_API_KEY` in `.env`.
- See `deploy/README.md` for the machine-specific shape: macOS runs Ollama natively (Docker cannot reach the Metal GPU); Windows runs vLLM in Docker under WSL2 (NVIDIA only, `--dtype half` mandatory on Turing cards). vLLM-Metal (MLX-backed, native install via `vllm-project/vllm-metal`) is a third macOS option — same `kind: vllm`, a different `base_url`.
- Match calls run concurrently, bounded per backend by `max_concurrency` (default 4 for `openrouter`, 1 for `ollama`/`vllm`, whose VRAM is the limit; raise it only with `OLLAMA_NUM_PARALLEL` / vLLM `--max-num-seqs` headroom). Only the model call runs on workers; scoring writes stay on the run's thread.
- Known gotcha: Ollama's default `qwen3:4b` tag "thinks" by default; its `<think>` block can exhaust a small `max_output_tokens` before an answer (`finish_reason=length`, empty `content`). Use `qwen3:4b-instruct` for small-budget roles like `triage`.
- Accounts (spec 015): each person has a folder `data/users/<name>/` with their own `jobs.db`; there is no shared `data/jobs.db` any more (the first account created adopts it). Sign-in is a username and passcode (scrypt in `auth.json`), one person signed in per server process, held in memory; the API answers 401 without the cookie. `adapters/accounts.py` is the folder logic, `adapters/api/auth.py` the routes and gate, `app._user_config()` the per-person config every router reads. CLI commands take the account with `career-copilot --user NAME <cmd>` (default: the only account). Tests run as a signed-in person by default; mark a test `real_auth` to exercise the gate.
- The CV is stored in the account's `jobs.db` (`cv_versions`), uploaded once on the CV page and versioned by the hash of its text; the match step scores against the current version. The upload is the only way a CV gets in: with none stored, `score`, `run` and `doctor` fail with "No CV is stored yet". A new CV never rescores existing jobs; `scores.cv_sha256` is provenance only. See `docs/superpowers/specs/010-persistence-and-lifecycle-design.md`.
- Job lifecycle (`job_decisions`, `job_views`): ✓ saves, ✕ dismisses, and a dismissed job is excluded from every list and from scoring for good. New (unviewed) and saved jobs sort first. `scoring.min_match_score` (default 45) is the bar a match must clear to count toward `initial_target` / `incremental_target`; the run stops after 3× the target in match calls (2× when the embedding ranker orders the candidates) and reports a shortfall rather than padding. See `docs/superpowers/specs/010-persistence-and-lifecycle-design.md`.
- `pii:` block (commented = defaults still apply): a Presidio-based redaction gateway sits in front of every provider call. It needs the `en_core_web_lg` spaCy model, not installed by `uv sync` — run `uv run python -m spacy download en_core_web_lg` once per machine.

## Runtime notes

- Run `career-copilot doctor` before `score` — it reports whether each role's backend is actually reachable, and whether the configured model is present.
- Run state lives in the `runs` table in the account's `jobs.db`. A run left in `state=running` (e.g. a crash outside the CLI's error-finalization path) blocks the next `score`/`run` with "a run is already active"; clearing it needs a direct store call, no CLI command exists yet.
- `career-copilot ui` serves the built frontend and API on one port. `ExportFiles` (a `StaticFiles` subclass in `adapters/api/static_ui.py`) maps extensionless paths to the exported `.html`, so a direct or refreshed request to a sub-route (`/recommendations`, `/resume`, `/settings`, `/preferences`) works.

## Working conventions

- Every code change goes on its own branch, named by conventional-commit type (`feat/`, `fix/`, `chore/`, `test/`, `docs/`). Never mix unrelated changes on one branch, never commit directly to `main`.
- A branch/PR can hold several commits; prefer small, easily reviewable PRs over one commit each. Do not commit until the user says so. Do not push. Do not open a PR unless asked.
- A pillar or long-running change gets a spec document first, following `docs/superpowers/specs/`. New specs are named `NNN-<topic>-design.md`, numbered in the order the topic was first designed (next free number), and follow the base-design-spec skill shape (Decisions, Shapes, Flow, Testing, Non-goals).
- Show the diff or a summary before committing — the user controls what gets committed and when.
- If a task needs a skill the user has no evidence for in this repo or their resume, say so — do not paper over the gap.
