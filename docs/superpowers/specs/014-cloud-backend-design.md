# Cloud Backend — Design

> A user without the hardware for good local models can enter an OpenRouter key once, on the Settings page, and run any role in the cloud. Three presets cover the common shapes (Local, Cloud, Hybrid); an Advanced table sets one role at a time, choosing from a curated per-role catalog or any capable OpenRouter model. The key lives in the OS keychain, never in SQLite or a file the app writes. Core rule: the app never moves a role to the cloud on its own. A down local backend stops the run and offers the switch; the user makes it. Builds on the local model runtime (008-local-model-runtime), the PII gateway (004-pii-gateway) and run budgets (002-scoring-and-digest).

## Decisions

| Topic | Decision |
|---|---|
| Provider | OpenRouter only. One key reaches every hosted model. `kind: openrouter` and its litellm prefix already exist (008). |
| Presets | `local`, `cloud`, `hybrid`. A preset is a profile choice, not new config: it names which `models:` overlay applies. |
| Local | The machine's auto profile (008): `mac-ollama`, `nvidia-vllm-awq`, or `config.yaml` as written. |
| Cloud | Profile `deploy/profiles/cloud-openrouter.yaml`, generated from the catalog's `recommended` entries: every role on `openrouter`, every role set explicitly. Token limits as the local profiles. |
| Hybrid | The local profile, then `match`, `assistant`, `profiler`, `curator`, `discovery` taken from the cloud profile. Triage, the high-volume cheap pass, stays local and free. |
| Catalog | `career_copilot/model_catalog.yaml`, packaged. Per role: one `recommended`, some `challenger`s, and a `baseline` (the Gemini 2.5 setup, kept for rollback). Each entry has price, capabilities, an optional `reserve_eur` and a one-line note. It is data: the model eval changes it, not code. |
| Defaults | Provisional until the eval runs. triage `deepseek/deepseek-v4-flash-0731`; match `google/gemini-3.8-flash`; profiler and curator `google/gemini-2.5-flash`; assistant `anthropic/claude-haiku-4.5`; discovery `google/gemini-3.8-flash`. Chosen for cost: DeepSeek V4 Flash input is about 1/20 of 2.5 Flash; 3.8 Flash costs more than 2.5 Flash, so only roles where quality pays get it. |
| Challengers | triage: `google/gemini-2.5-flash`, `typesafe/jev-router`. match: `anthropic/claude-sonnet-5.5`, `google/gemini-2.5-pro`. profiler: `google/gemini-3.8-flash`. curator, discovery: `anthropic/claude-haiku-4.5`. assistant: `google/gemini-3.8-flash`, `anthropic/claude-sonnet-5.5`. |
| Jev | `typesafe/jev-router` is an ordinary chat model and is offered as a challenger. TypeSafe's Decisions API (`jev-1.13`: typed choice or score, no free-text reason) does not fit the chat path and gets its own spec. |
| Capability gate | Saving a role checks the model's OpenRouter `supported_parameters`: `tools` for `curator`, `assistant`, `discovery`; `response_format` for `triage`, `match`, `profiler`. A failing model is refused, naming the missing parameter. |
| Unknown price | A model priced dynamically (`jev-router` lists −1) has no upfront price for the cost meter to reserve against. It is accepted only with a `reserve_eur` from the catalog or the override. |
| Real cost | Cloud calls ask OpenRouter for usage accounting and charge the meter its returned `usage.cost`. litellm's own price map reports 0 for models it does not know, which would let a run pass its budget. |
| Role fallback | Today `profiler`, `curator`, `assistant`, `discovery` fall back to the triage model. The fallback stays only when the triage model passes that role's capability gate; otherwise the role must be set, and `doctor` names it. |
| Advanced | Per-role override of `backend` and `model`, applied after the preset. An override is kept until cleared; changing preset does not drop it, and the UI marks overridden roles. |
| Precedence | 1: env `CAREER_COPILOT_PROFILE` (dev). 2: saved model settings. 3: `models.profile`. 4: auto. Then saved per-role overrides. `ProfileChoice.source` gains `settings`. |
| Where saved | SQLite table `model_settings`, append-only and versioned like `preferences`. The newest row is in force. No key in it. |
| When read | At each run start, health check and assistant session, not at process start. A change applies on the next run with no restart. A running run keeps the roles it started with. |
| Key store | `SecretStore` port. `EnvSecretStore` reads `OPENROUTER_API_KEY` (from `.env` or the shell). `KeyringSecretStore` uses `keyring` (macOS Keychain, Windows Credential Manager), service `career-copilot`, name `openrouter`. `keyring` is a new runtime dependency. |
| Key order | Env first, then keychain. Env wins so a developer's `.env` keeps working; the UI says which source is in force. |
| No keychain | Headless Linux/WSL may have no keyring backend. Saving then fails with "no OS keychain; put OPENROUTER_API_KEY in .env". No plaintext fallback. |
| Key over API | Write-only. `PUT` stores it; `GET` returns `{set, source, tail}` with the last 4 characters only. `DELETE` clears the keychain entry, never `.env`. |
| Key test | `POST .../test` calls OpenRouter `GET /api/v1/key`: valid, credit limit, usage. Spends nothing. Run before saving; an invalid key is not stored. |
| Model list | Each role's picker lists the catalog entries first, marked recommended, challenger or baseline. Below them, "Any model" searches OpenRouter `GET /api/v1/models`, filtered by the role's capability gate and cached in memory for one hour. |
| Price shown | Every entry shows input and output price per million tokens from the live list, so a stale catalog price never shows. A catalog id missing from the live list is greyed out with "no longer on OpenRouter". |
| Down local | `diagnose` already stops a run before the first job (008). The report gains `suggest`: `hybrid` or `cloud` if a key is set, `add_key` if not. The UI shows the matching button. Never an automatic switch. |
| Consent | The first switch to a preset or override that puts a role on `openrouter` shows a one-time notice: the CV and jobs leave the machine, redacted. Accepting is saved with the settings row. |
| Privacy badge | `privacy-badge.tsx` shows "Local only" or "Cloud: N roles (redacted)" from the roles in force. |
| Redaction | Unchanged. Every cloud call goes through `RedactingLLMClient`; `pii.enabled: false` stays the only way out. |
| Budget | Unchanged. `scoring.max_eur_per_run` caps a cloud run; Settings shows the cap and the last run's spend read-only. Editing it belongs to a later settings-in-store spec. |
| CLI | `career-copilot models show`, `models preset <local|cloud|hybrid>`, `models key set` (hidden prompt, tests first), `models key clear`. Same store and secret port as the API. |

## Shapes

```python
# domain/model_settings.py
Preset = Literal["local", "cloud", "hybrid"]

class RoleOverride(BaseModel):
    backend: str                  # a name in models.backends, or "openrouter"
    model: str

class CatalogEntry(BaseModel):    # one line of model_catalog.yaml
    id: str                       # OpenRouter model id
    roles: list[str]
    tier: Literal["recommended", "challenger", "baseline"]
    caps: list[Literal["tools", "response_format", "structured_outputs"]]   # last one used by a later spec
    reserve_eur: float | None = None   # required when the live price is dynamic
    note: str

class ModelSettings(BaseModel):
    version: int
    preset: Preset | None         # None: no saved choice, fall through to config/auto
    overrides: dict[str, RoleOverride] = {}
    cloud_consent: bool = False

# ports.py
class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...   # raises SecretStoreUnavailable
    def delete(self, name: str) -> None: ...

class ChainedSecretStore:         # env, then keychain; reports which answered
    def get_with_source(self, name: str) -> tuple[str | None, Literal["env", "keychain", "none"]]: ...

# config.py
def resolve_models(config: Config, settings: ModelSettings | None, env: Mapping[str, str]) -> Config: ...
```

```sql
CREATE TABLE model_settings (
  version INTEGER PRIMARY KEY, preset TEXT, overrides TEXT NOT NULL,  -- JSON
  cloud_consent INTEGER NOT NULL DEFAULT 0, saved_at TEXT NOT NULL
);
```

| Endpoint | Body / answer |
|---|---|
| `GET /api/settings/models` | settings in force, resolved roles (kind, model, source: preset/override/config), key status, cap |
| `PUT /api/settings/models` | `{preset, overrides, cloud_consent}`; 409 if a cloud role and no key, 428 if consent missing |
| `GET /api/settings/openrouter-key` | `{set, source, tail}` |
| `PUT` / `DELETE` same path | store after a passing test / clear keychain |
| `POST /api/settings/openrouter-key/test` | `{valid, limit, usage, detail}`; key in body or the stored one |
| `GET /api/settings/openrouter-models?role=` | catalog entries for the role (tier, live price, note), then the gated live list |

## Flow

```
Config.load(path)                          # unchanged; yields the base config
resolve_models(base, store.model_settings(), env):
    choice = env profile  or  settings.preset -> profile name  or  models.profile  or  auto
        local  -> auto pick (008)
        cloud  -> "cloud-openrouter"
        hybrid -> auto pick, then cloud-openrouter's match/assistant/profiler/curator
    data = apply_profile(base, overlay(choice))           # existing merge
    for role, o in settings.overrides: data.models.roles[role] = {backend, model, tokens kept}
    return validated Config with profile.source = "settings" when step 2 decided

run start / health / assistant:
    config = resolve_models(_config, store.model_settings(), os.environ)
    report = diagnose(config, secrets)                    # openrouter reachable = key found
    if not report.ok: 409 {problem, suggest}              # suggest: hybrid | cloud | add_key
    build_client(role, secrets)                           # key from ChainedSecretStore, not os.environ

Settings page:
    Model section: preset radio (Local / Cloud / Hybrid), role table, privacy badge, budget line
    pick cloud/hybrid -> no key? key dialog -> test -> save to keychain
                      -> first time? consent notice -> PUT settings
    Advanced (collapsed): per role picker, catalog first, then any model
        -> capability gate -> unknown price needs reserve_eur -> PUT settings
```

## Testing

| Layer | What it proves |
|---|---|
| CI unit (`test_model_settings.py`) | Each preset resolves to the expected roles on injected `apple_silicon`, `nvidia`, `other`. Hybrid keeps triage local. Overrides win over the preset and survive a preset change. |
| CI unit (`test_model_settings.py`) | Precedence: env profile beats saved settings beats `models.profile` beats auto. `source` is `settings` only when step 2 decided. |
| CI unit (`test_model_catalog.py`) | The shipped catalog validates: one `recommended` per role, every role has a `baseline`, every entry's `caps` pass its roles' gate. Against a stubbed live list: a missing id is flagged, a dynamic price without `reserve_eur` is refused, a model without `tools` is refused for `assistant`. |
| CI unit (`test_model_settings.py`) | The Cloud profile sets every role explicitly. A triage model without `tools` stops the fallback for `curator`, and `doctor` names the role. |
| CI unit (`test_litellm_client.py`) | A cloud reply's `usage.cost` is what the meter is charged, also when litellm's price map says 0. |
| CI unit (`test_secret_store.py`) | Env beats keychain; source is reported. A fake keyring with no backend raises `SecretStoreUnavailable`; nothing is written elsewhere. `DELETE` never touches env. |
| CI unit (`test_model_backends.py`) | `build_client` takes the key from the secret store; a cloud role with no key still fails at build, naming the role. |
| CI unit (`test_doctor.py`) | A down local role yields `suggest=hybrid` with a key, `add_key` without one. A cloud role is never chosen for the user. |
| CI contract (`test_model_settings_api.py`) | Settings round-trip and versioning; 409 without key; 428 without consent; key `GET` never returns more than the tail; test endpoint against a stub OpenRouter transport (valid, 401, network error). |
| CI contract (`test_runs_api.py`) | A settings change between runs takes effect on the next run without restart; a running run keeps its roles. |
| CI frontend (vitest) | Preset form, key dialog states, consent notice shown once, override marker, badge text. |
| Manual | On the 1660 Ti machine: save a key in the UI, switch to Hybrid, run `score`; `doctor` names the source `settings`; `scripts/verify_pii_boundary.py` shows no identity in cloud payloads; run spend stays under the cap. |

The suite makes no network calls: OpenRouter is a stub transport, the keychain a fake `keyring` backend.

## Non-goals

TypeSafe's Decisions API for triage (its own spec), choosing the defaults by measurement (a later model-eval spec), call concurrency and strict JSON schemas (a later spec), providers other than OpenRouter, direct provider keys, custom OpenAI-compatible URLs, automatic fallback to the cloud, per-job or cost-based routing, editing the run budget in the UI (a later spec), syncing settings across machines, encrypting the key in SQLite.
