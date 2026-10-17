# Running the models locally

Two machines, two runtimes, and no config edit between them. Nothing here starts
from inside the app: a model server is a service you bring up, and
`career-copilot doctor` tells you what it found.

## Which profile

The `models:` setup for each machine lives in `deploy/profiles/`. At start-up the app
picks one and overlays its backends and roles on `config.yaml`; everything else in the
config is untouched.

| Machine | Detected by | Profile |
|---|---|---|
| macOS on Apple Silicon | `platform` reports Darwin / arm64 | `mac-ollama` |
| NVIDIA GPU (Windows, WSL2, Linux) | `nvidia-smi` exits 0 within 2 s | `nvidia-vllm-awq` |
| anything else | — | none: `config.yaml` as written |

`doctor` and `ui` print the choice and why, e.g.
`Profile: nvidia-vllm-awq (auto: NVIDIA GPU found)`; `GET /api/health/models` returns it
as `profile: {name, source}`.

To override, first match wins:

1. `CAREER_COPILOT_PROFILE=mac-ollama` in the environment (or `.env`).
2. `models.profile: mac-ollama` in `config.yaml` (default `auto`).
3. Auto-detection.

`none` at either level turns profiles off. A name that is not a file in
`deploy/profiles/` stops start-up and lists the real ones. To change a model for one
machine, edit its profile file.

## macOS (M4 Max) — Ollama

Docker on macOS cannot reach Metal, so a containerised server would run on the CPU.
Ollama runs natively and uses the GPU.

```bash
brew install ollama
ollama serve                    # or the menu-bar app
ollama pull qwen3:4b            # triage
ollama pull qwen3:14b           # match
```

Pull `qwen3:4b-instruct` too: `mac-ollama` uses it for triage and the agents, because
plain `qwen3:4b` "thinks" and can spend a 256-token triage budget before answering.

```bash
ollama pull qwen3:4b-instruct
```

## Windows (GTX 1660 Ti) — vLLM in Docker under WSL2

Needs WSL2, Docker Desktop with WSL integration, and the NVIDIA driver on Windows —
not inside WSL. Check the GPU is visible first:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
docker compose -f deploy/vllm/docker-compose.yml up
```

The card is Turing with 6 GB: `--dtype half` is required (no bf16, no FP8), and the
model has to leave room for the KV cache. The compose file serves a 4-bit AWQ build of
Qwen3-4B-Instruct-2507 (about 2.5 GB of weights; fp16 would be about 8 GB and cannot
fit) under the name `qwen3-4b-instruct`. It listens on host port 8001, so port 8000
stays free for `career-copilot ui`. If vLLM exits with a CUDA OOM, lower
`--max-model-len` before reaching for a smaller model.

`nvidia-vllm-awq` points every role at that model on port 8001.

If vLLM refuses sm_75 or the AWQ kernels on Turing, fall back to Ollama for Windows:
`CAREER_COPILOT_PROFILE=mac-ollama` (with `qwen3:4b-instruct` pulled), or write the roles
into `config.yaml` with `models.profile: none`.

The Python side runs as a normal `uv run` on Windows; only HTTP crosses into WSL2.

## Keeping a remote role

A role is independent, so the cheap pass can be local and the expensive one remote.
Turn profiles off so `config.yaml` is used as written:

```yaml
models:
  profile: none
  roles:
    triage: {backend: ollama,     model: qwen3:4b}
    match:  {backend: openrouter, model: google/gemini-2.5-pro}
```

`openrouter` still needs `OPENROUTER_API_KEY` in `.env`. Redaction applies to local and
remote roles alike — see `PII_HANDLING.md`.

## Checking

```bash
uv run career-copilot doctor
uv run career-copilot ui        # API + built frontend on one port
```

`ui` serves `src/frontend/out`, so build it once: `cd src/frontend && npm install && npm run build`.

## Comparing backends

Run the same day's pipeline on each machine, then export and compare the top lists:

```bash
uv run career-copilot export-top --out data/top-vllm.json      # Windows, vLLM
uv run career-copilot export-top --out data/top-ollama.json    # Mac, Ollama
uv run career-copilot compare data/top-vllm.json data/top-ollama.json
```

`compare` prints the shared jobs, the Jaccard overlap, the rank correlation and the mean
score difference on shared jobs, and lists the jobs only one backend recommended. Jobs
are matched by canonical URL, so both machines should crawl the same boards.
