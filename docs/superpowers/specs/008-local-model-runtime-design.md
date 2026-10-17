# Local Model Runtime — Design

> Every model role runs on the user's own machine by default: Ollama on the Mac, vLLM in Docker on the NVIDIA box, OpenRouter as an opt-in. The app picks the setup for the machine by itself, and `doctor` and the UI always say what was picked and why. Core rule: the app never starts a model server; it only checks one and says how to start it. Builds on the PII gateway (004-pii-gateway), the scoring roles (002-scoring-and-digest) and `deploy/README.md`.

## Decisions

| Topic | Decision |
|---|---|
| Default path | A run needs no API key and no network. `openrouter` is used only when a role names it. |
| Roles and backends | `models:` in `config.yaml` names backends once and points each role at one. A machine differs from another only by which backend its roles name. |
| Backend kinds | `ollama`, `vllm`, `openrouter`. `kind` decides the litellm prefix (`ollama_chat`, `hosted_vllm`, `openrouter`) and whether a key is needed. Only `openrouter` needs `OPENROUTER_API_KEY`. |
| Roles | `triage`, `match`, `profiler`, `curator`, `assistant`, `discovery`. A missing `profiler`, `curator`, `assistant` or `discovery` role uses the `triage` model. |
| Old config | With no `models.roles` entry, `triage` and `match` come from `scoring.triage_model` and `scoring.match_model`. Such a config keeps its remote run. |
| One client | `LiteLLMClient` serves every kind. `adapters/llm/backends.py` is the only file that writes a litellm prefix or a probe URL. A local call reports cost 0. |
| Local key | Local backends get the dummy key `local`. A remote role with no key fails when the client is built, not mid-run. |
| Redaction | Redaction stays on for local calls. `pii.enabled: false` is the one switch that turns it off. The optional `pii.llm_pass` uses the same Ollama and is allowed only on a loopback host. |
| Run bounds | Euros do not bind a local run, so `RunLimits` adds `max_steps` and `max_seconds`. `CostMeter.step` refuses to start a step past either. Zero means no ceiling. |
| Profile | A YAML file in `deploy/profiles/` holding only a `models:` block. Two ship: `mac-ollama`, `nvidia-vllm-awq`. |
| Hardware | `detect_hardware()` returns `apple_silicon` (Darwin and arm64), `nvidia` (`nvidia-smi` exits 0 within 2 seconds), or `other`. Any detection error is `other`. |
| Choice order | 1: env `CAREER_COPILOT_PROFILE`. 2: `models.profile` (default `auto`). 3: auto by hardware. `none` at either level turns profiles off. |
| Auto | `apple_silicon` picks `mac-ollama`. `nvidia` picks `nvidia-vllm-awq`. `other` picks none, so `config.yaml` is used as written. Auto never fails. |
| Merge | A profile's backends and roles replace the same-named entries. Everything else in the config is untouched. |
| Bad profile | An unknown name stops start-up and lists the real names. A file that does not parse or validate stops start-up and names the file. |
| Mac profile | Backend `ollama`. Every role but `match` uses `qwen3:4b-instruct`; `match` uses `qwen3:14b`. Triage has 256 output tokens, all others 1024. |
| Triage model | Triage uses `qwen3:4b-instruct`, not `qwen3:4b`. The plain tag "thinks" and its `<think>` block can use the 256-token budget before any answer. |
| NVIDIA profile | Backend `vllm` at `http://localhost:8001/v1`. All five roles use `qwen3-4b-instruct`, the 4-bit AWQ build of Qwen3-4B-Instruct-2507 served by `deploy/vllm/docker-compose.yml`. |
| Agent roles | Both profiles set `profiler`, `curator` and `assistant` at 1024 tokens. A fallback to `triage` would give them 256. |
| Ports | Compose publishes vLLM on host port 8001. Port 8000 stays free for `career-copilot ui`. |
| GPU limits | The GTX 1660 Ti is Turing with 6 GB: `--dtype=half` is required (no bf16, no FP8), `--max-model-len=8192`, `--gpu-memory-utilization=0.85`. On a CUDA out-of-memory, lower the context before a smaller model. |
| Doctor | `career-copilot doctor` prints the profile line, one line per role (reachable, model present, tool calling yes/no/unknown), a redaction line and a CV line. It exits 1 if a role, redaction or the CV is unusable. |
| Probe | `diagnose` probes `triage`, `match`, `profiler`, `curator`, `assistant`. Roles on the same kind, base URL and model are probed once. A probe never raises; a down backend is a report. |
| Down backend | `doctor` prints the start command once per server: `ollama serve`, or the `docker compose` line. The app never runs it. |
| Pre-run check | `score` and `run` call `diagnose` first. A role that cannot be served stops the run before the first job, naming role, kind, model and reason. |
| Remote probe | An `openrouter` role is "reachable" if its key is set. No request is sent. |
| Visible | `ui` prints the same lines at start. `GET /api/health/models` returns `ok`, `profile: {name, source}` and per role `kind`, `model`, `base_url`, `reachable`, `model_present`, `tools`, `detail`, `local`. `app_sessions.profile` stores the name. |
| Launcher | `career-copilot ui` serves the built frontend and API on one port (default 8000, `--host`, `--port`, `--open/--no-open`). A down backend or missing redaction model warns but does not stop the UI. |
| Static files | `ExportFiles` serves `/resume` from `resume.html`. A missing build returns "API only" with the build command. |
| Compare | `export-top` writes the top matches to JSON per machine. `compare` prints shared jobs, overlap, rank correlation and mean score difference. Jobs match by canonical URL. |

## Shapes

```python
# config.py
class BackendSpec(BaseModel):   # one model server
    kind: Literal["ollama", "vllm", "openrouter"]
    base_url: str | None

class RoleSpec(BaseModel):      # what serves one role
    backend: str
    model: str
    max_output_tokens: int = 1024
    reserve_eur: float | None   # None: take the role's reserve from `scoring:`

class ModelsSpec(BaseModel):
    backends: dict[str, BackendSpec]
    roles: dict[str, RoleSpec]
    profile: str = "auto"       # file name, "auto" or "none"

# adapters/profiles.py
class Hardware:       kind: Literal["apple_silicon", "nvidia", "other"]; detail: str
class ProfileChoice:  name: str | None; source: Literal["env", "config", "auto", "none"]; reason: str

# adapters/llm/backends.py
class BackendStatus:  # what a probe found; tools is True, False or None (unknown)
    role: str; kind: str; model: str; reachable: bool; model_present: bool; tools: bool | None

# domain/budget.py
class RunLimits:      max_steps: int = 0; max_seconds: float = 0.0
```

```yaml
# deploy/profiles/nvidia-vllm-awq.yaml (abridged)
models:
  backends: { vllm: {kind: vllm, base_url: "http://localhost:8001/v1"} }
  roles:
    triage: {backend: vllm, model: qwen3-4b-instruct, max_output_tokens: 256}
    match:  {backend: vllm, model: qwen3-4b-instruct, max_output_tokens: 1024}
```

## Flow

```
Config.load(path):
    data = yaml(path)
    choice = choose_profile(env, data.models.profile or "auto", detect_hardware, names(deploy/profiles))
        # unknown name -> ProfileError listing real names
    if choice.name: data = apply_profile(data, load_profile(file))   # bad file -> ProfileError naming it
    config = validate(data); config.profile = choice

config.role(name):
    spec = models.roles[name]
    if no spec and name in (discovery, profiler, curator, assistant): return role("triage") renamed
    if no spec: parse scoring.triage_model / match_model into kind + model
    backend = models.backends[spec.backend]   # missing -> ValueError naming the role
    return ResolvedRole(litellm_model = prefix(kind) + "/" + model, ...)

score / run:
    report = diagnose(config)           # probe each distinct (kind, base_url, model) once
    if not report.ok: print problem(); exit 1     # before any job is loaded
    for role: client = build_client(role)         # remote role with no key -> BackendUnavailable
    wrap in redaction; loop jobs under CostMeter.reserve and CostMeter.step

probe(role):
    openrouter -> reachable = key is set
    ollama     -> GET /api/tags; model absent -> detail "run: ollama pull <model>"; POST /api/show for tools
    vllm       -> GET /models; model absent -> detail lists served ids
    network error -> unreachable report, no exception
```

## Testing

| Layer | What it proves |
|---|---|
| CI unit (`test_profiles.py`) | Injected macOS arm64 gives `mac-ollama`; `nvidia-smi` exit 0 gives `nvidia-vllm-awq`; a missing or timed-out `nvidia-smi` gives no profile. |
| CI unit (`test_profiles.py`) | Env beats config, config beats auto, `none` at either level gives no profile. An unknown name lists the real names; a bad file names itself. Merge leaves `scoring`, `filters`, `discovery` unchanged. |
| CI unit (`test_config.py`) | A config with no `models:` still resolves `triage` and `match` from `scoring:`. Each kind gets the right litellm prefix. |
| CI unit (`test_model_backends.py`) | Probes against a stub transport: reachable, unreachable, model missing, tools unknown. A remote role without a key fails at build. |
| CI unit (`test_doctor.py`) | A fake prober; five roles on one model call the backend once; `problem()` names each unusable role. |
| CI unit (`test_budget.py`) | The step and wall-clock ceilings refuse the next step; zero means no ceiling. |
| CI unit (`test_local_backend_boundary.py`) | A local role is still wrapped in the redaction boundary; `pii.enabled: false` is the only way out. |
| CI unit (`test_static_ui.py`) | A built frontend is served at the root with API routes still winning; an unbuilt one leaves the API running. |
| CI contract (`test_health_api.py`) | `/api/health/models` carries the profile, roles and `local` flag. |
| CI e2e (`test_cli_doctor_profile.py`) | `doctor` prints the profile line for each source. |
| Manual | On each machine run `career-copilot doctor` with no edit to `config.yaml`: the right profile is named and a `score` run uses that backend. `scripts/verify_pii_boundary.py` checks no identity reaches a backend. |

CI (`.github/workflows/ci.yml`) runs ruff, mypy and pytest with no model server and no network. Correctness is judged by injected hardware, transports and probers; no test depends on the real machine.

## Non-goals

Starting or supervising model servers, downloading models, tuning model size by VRAM, fine-tuning, AMD or Intel profiles, a packaged desktop binary.
