# Career Copilot

[![CI](https://github.com/Shrinidhibhat87/career-copilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Shrinidhibhat87/career-copilot/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-000000?logo=nextdotjs&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)

![Ollama](https://img.shields.io/badge/Ollama-000000?logo=ollama&logoColor=white)
![vLLM](https://img.shields.io/badge/vLLM-30A2FF?logo=vllm&logoColor=white)
![OpenRouter](https://img.shields.io/badge/OpenRouter-6467F2?logoColor=white)

A personal job-search agent. It discovers postings from ATS boards, hard-filters them,
scores the survivors with LLMs, and renders a ranked digest. It recommends roles; it
never submits an application.

## Quick start

Requires [uv](https://docs.astral.sh/uv/), Node.js, and a model backend: local
(Ollama/vLLM, needs a capable GPU or Apple Silicon) or cloud (an
[OpenRouter](https://openrouter.ai/keys) key, no special hardware).

**Step 1: Install and build**

```bash
git clone https://github.com/Shrinidhibhat87/career-copilot.git
cd career-copilot

uv sync
uv run python -m spacy download en_core_web_lg   # PII redaction model, once per machine
cp .env.example .env                             # optional: OPENROUTER_API_KEY for cloud models
cd src/frontend && npm install && npm run build && cd ../..   # build the UI, once
```

**Step 2: Start the app** (API and UI on one port, opens your browser)

```bash
uv run career-copilot ui
```

**Step 3: Create your account** on the sign-in page (a username and a passcode; they stay on this
machine). Each person on the machine gets their own account and their own data in
`data/users/<name>/`. If you used the app before accounts existed, your first account takes over
that data. The passcode guards the app, not the file, and there is no reset. To move to another
machine, use Settings → Your data → Download my data, then on the new machine choose "Import from
an export file" while creating the account (the OpenRouter key is not in the file).

**Step 4: Upload your CV** on the CV page.

**Step 5: Choose your models** in Settings → Models:

- Pick a Local, Hybrid or Cloud preset, or override a single role.
- For cloud, paste your OpenRouter key; it is tested before it is stored.
- For local, set up Ollama or vLLM first: see [`deploy/README.md`](deploy/README.md).
- `config.yaml` `models:` holds the defaults.

To check from the terminal that the backends are reachable: `uv run career-copilot doctor`.
Terminal commands work on one account: `uv run career-copilot --user anna doctor` (the only
account is the default when there is just one).

## Run the agent (CLI)

```bash
uv run career-copilot discover   # fetch configured boards, filter, store
uv run career-copilot score      # triage everything, deep-score the top N
uv run career-copilot digest     # render data/digest.md
uv run career-copilot run        # discover → score → digest
uv run career-copilot list --outcome pass --limit 20
```

Add a company by appending its ATS board slug to `targets` in `config.yaml`.

## Frontend development

`career-copilot ui` serves the built frontend. To work on the frontend with hot reload,
run the API and the dev server in two terminals:

```bash
uv run uvicorn career_copilot.adapters.api.app:app --reload   # API on :8000
cd src/frontend && npm run dev                                # UI on http://localhost:3000
```

Sign-in is a same-origin cookie, so the hot-reload setup cannot sign in; use `career-copilot ui`
for anything behind the sign-in. The frontend talks to `http://127.0.0.1:8000`; set `NEXT_PUBLIC_API_URL` (frontend) and
`FRONTEND_ORIGIN` (API) to change that.

## Checks

```bash
uv run pytest && uv run ruff check . && uv run mypy
cd src/frontend && npm run lint && npm run typecheck && npm run test
```

## Docs

- [`docs/architecture.md`](docs/architecture.md) — design and components
- [`docs/job_search_agent_plan.md`](docs/job_search_agent_plan.md) — goals and scope
- [`deploy/README.md`](deploy/README.md) — running models locally (Ollama, vLLM)
- [`PII_HANDLING.md`](PII_HANDLING.md) — redaction boundary
