"""Configuration loading. No profile identity lives in code."""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, PrivateAttr

from career_copilot.adapters.profiles import (
    AUTO,
    CLOUD_PROFILE,
    ENV_VAR,
    NO_PROFILE,
    Hardware,
    ProfileChoice,
    ProfileError,
    apply_profile,
    available_profiles,
    choose_profile,
    detect_hardware,
    load_profile,
)
from career_copilot.domain.filters import FilterConfig
from career_copilot.domain.model_settings import HYBRID_CLOUD_ROLES, ModelSettings
from career_copilot.domain.models import SourceTarget
from career_copilot.domain.scoring import RUBRIC_VERSION


class TargetSpec(BaseModel):
    source: str
    identifier: str
    tier: int = 0
    cooldown_hours: int = 24

    def to_target(self) -> SourceTarget:
        return SourceTarget(
            source=self.source,
            identifier=self.identifier,
            tier=self.tier,
            cooldown_hours=self.cooldown_hours,
        )


class FilterSpec(BaseModel):
    """The bootstrap seed for `domain.preferences.Preferences`.

    Nothing in a run reads this once preferences have been saved: it seeds the in-memory
    bootstrap at version 0 and is then out of the loop. Editing it after a save changes
    nothing, which is why `config.yaml` marks the block as a seed.
    """

    allowed_cities: list[str] = Field(
        default_factory=lambda: [
            "berlin",
            "munich",
            "hamburg",
            "cologne",
            "frankfurt",
            "remote",
            "",
        ]
    )
    preferred_cities: list[str] = Field(default_factory=lambda: ["berlin"])
    flagged_cities: list[str] = Field(default_factory=lambda: ["munich"])
    allow_remote: bool = True
    require_permanent: bool = True
    excluded_role_terms: list[str] = Field(
        default_factory=lambda: list(FilterConfig().excluded_role_terms)
    )
    title_terms: list[str] = Field(default_factory=lambda: list(FilterConfig().title_terms))
    core_stack_terms: list[str] = Field(
        default_factory=lambda: list(FilterConfig().core_stack_terms)
    )
    excluded_stack_terms: list[str] = Field(
        default_factory=lambda: list(FilterConfig().excluded_stack_terms)
    )
    max_per_company: int = Field(default=2, ge=1)

    def to_filter_config(self) -> FilterConfig:
        return FilterConfig(
            allowed_cities=frozenset(self.allowed_cities),
            preferred_cities=frozenset(self.preferred_cities),
            flagged_cities=frozenset(self.flagged_cities),
            allow_remote=self.allow_remote,
            require_permanent=self.require_permanent,
            excluded_role_terms=tuple(self.excluded_role_terms),
            title_terms=tuple(self.title_terms),
            core_stack_terms=tuple(self.core_stack_terms),
            excluded_stack_terms=tuple(self.excluded_stack_terms),
            max_per_company=self.max_per_company,
        )


class ScoringSpec(BaseModel):
    """Model ids are OpenRouter ids, so swapping models is configuration."""

    initial_target: int = 50
    incremental_target: int = 15
    # The bar a match must clear to count toward the targets above (45 is the "possible"
    # band, see `ScoreVerdict`). Below it a job is still scored and stored, just not counted.
    min_match_score: int = Field(default=45, ge=0, le=100)
    triage_model: str = "openrouter/google/gemini-2.5-flash"
    match_model: str = "openrouter/google/gemini-2.5-pro"
    max_eur_per_run: float = Field(default=1.0, gt=0)
    triage_max_output_tokens: int = Field(default=256, ge=1)
    match_max_output_tokens: int = Field(default=1024, ge=1)
    triage_reserve_eur: float = Field(default=0.02, gt=0)
    match_reserve_eur: float = Field(default=0.20, gt=0)
    rubric_version: int = RUBRIC_VERSION
    digest_path: str = "data/digest.md"
    # Local CPU embedder for ranking the pool against sample roles and intent. Changing it
    # re-embeds every posting once, since cached vectors are keyed by model.
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # How much of the ranking score is the job's similarity to the stored CV; the rest is
    # similarity to sample roles and intent. 0 ignores the CV.
    cv_weight: float = Field(default=0.3, ge=0, le=1)
    # How candidates are ordered for the match step. `embedding` ranks the pool on the CPU
    # and lets the curator agent shortlist from it; `llm_triage` is the old path, one LLM
    # call per candidate. The embedding path falls back to triage when it cannot rank.
    ranker: Literal["embedding", "llm_triage"] = "embedding"
    # Whether the curator agent reads the ranked pool; off means pure embedding order.
    curate: bool = True


class ObservabilitySpec(BaseModel):
    """Call recording and app logs. All defaults; an omitted block keeps recording on."""

    log_days: int = Field(default=14, ge=1)
    detail_days: int = Field(default=90, ge=1)
    archive_logs: bool = False


class LifecycleSpec(BaseModel):
    """What happens to a dismissed job. It sits in the recycle bin for `bin_days`, then is
    cleaned for good."""

    bin_days: int = Field(default=7, ge=1, le=90)


class PiiSpec(BaseModel):
    """Redaction gateway config (design doc §13). An omitted `pii:` block must not disable
    redaction — every field below is a default, not a requirement to opt in."""

    enabled: bool = True
    entities: list[str] = Field(
        default_factory=lambda: [
            "PERSON",
            "EMAIL",
            "PHONE",
            "ADDRESS",
            "URL",
            "IBAN",
            "CREDIT_CARD",
            "IP",
        ]
    )
    # Opt in; llm_pass + llm_failure: block would make a running Ollama a hard
    # precondition for scoring at all, which is the wrong default (§13).
    llm_pass: bool = False
    llm_endpoint: str = "http://localhost:11434"
    llm_allow_remote: bool = False
    language: str = "en"
    llm_model: str = "qwen2.5:3b"
    llm_timeout_seconds: int = 30
    llm_failure: Literal["block", "presidio_only"] = "block"
    outbound: Literal["allow", "warn", "block"] = "warn"
    # PERSON precision (person-precision design §6). The defaults are the policy: both
    # switches exist to debug a suspected mis-derivation, not to be turned on later.
    owner_anchor: bool = True
    drop_single_token_person: bool = True
    person_false_positive_terms: list[str] = Field(default_factory=list)
    # Escape hatch for what derivation cannot see — a maiden name, a second phone number
    # that never appears in the resume. Absent by default, and silent when absent.
    identity_path: str | None = None


class BackendSpec(BaseModel):
    """One model server. `kind` decides the litellm prefix and whether a key is needed."""

    kind: Literal["ollama", "vllm", "openrouter"]
    base_url: str | None = None
    # Model calls in flight at once. Absent means 4 for openrouter and 1 for a local server,
    # whose VRAM is the limit: each extra request costs KV-cache memory.
    max_concurrency: int | None = Field(default=None, ge=1, le=32)

    def key_env(self) -> str | None:
        return "OPENROUTER_API_KEY" if self.kind == "openrouter" else None

    def concurrency(self) -> int:
        if self.max_concurrency is not None:
            return self.max_concurrency
        return 4 if self.kind == "openrouter" else 1


class RoleSpec(BaseModel):
    """What a job in the pipeline — triage, match — is served by."""

    backend: str
    model: str
    max_output_tokens: int = Field(default=1024, ge=1)
    # Absent means "take the role's reserve from `scoring:`", which keeps a remote run
    # billed exactly as before. A local backend reports €0 and the reserve is moot.
    reserve_eur: float | None = Field(default=None, ge=0)


_LITELLM_PREFIX = {"ollama": "ollama_chat", "vllm": "hosted_vllm", "openrouter": "openrouter"}

_DEFAULT_BACKENDS = {
    "ollama": BackendSpec(kind="ollama", base_url="http://localhost:11434"),
    "vllm": BackendSpec(kind="vllm", base_url="http://localhost:8000/v1"),
    "openrouter": BackendSpec(kind="openrouter"),
}


@dataclass(frozen=True, slots=True)
class ResolvedRole:
    """A role with its backend folded in: everything a client needs and nothing else."""

    name: str
    kind: str
    model: str
    litellm_model: str
    base_url: str | None
    max_output_tokens: int
    reserve_eur: float
    key_env: str | None
    max_concurrency: int = 1

    @property
    def is_local(self) -> bool:
        return self.kind in {"ollama", "vllm"}


class ModelsSpec(BaseModel):
    """Backends named once, roles pointing at them.

    Omitting the block entirely is supported and means "keep using `scoring:`": the two
    Gemini ids there still describe a working remote run, and a machine that has not
    been given a local runtime should not be broken by the arrival of one.
    """

    backends: dict[str, BackendSpec] = Field(default_factory=lambda: dict(_DEFAULT_BACKENDS))
    roles: dict[str, RoleSpec] = Field(default_factory=dict)
    # A file name in `deploy/profiles/`, `auto` to pick one by hardware, or `none` to use
    # this block exactly as written. `CAREER_COPILOT_PROFILE` overrides it.
    profile: str = "auto"


class DiscoverySpec(BaseModel):
    """Ceilings for the source-discovery agent.

    Separate from `scoring:` because they bound a different kind of run: scoring is
    priced per job, discovery is a loop whose length is not known before it starts.
    """

    target_proposals: int = Field(default=3, ge=1)
    max_searches: int = Field(default=6, ge=1)
    max_eur_per_run: float = Field(default=0.25, gt=0)
    max_requests: int = Field(default=12, ge=1)
    max_tool_calls: int = Field(default=24, ge=1)
    max_seconds: float = Field(default=600.0, gt=0)


class NotionListSpec(BaseModel):
    """The one Notion database to sync. The token itself stays in `.env`, never here."""

    database_id: str = ""


class NotionSpec(BaseModel):
    """Where applied jobs are logged in Notion. The app searches the pages shared with the
    integration for `applications_parent_title` and finds or creates the database inside."""

    applications_parent_title: str = "Career Copilot"
    applications_database_title: str = "Applications"


class CsvListSpec(BaseModel):
    path: str = ""


class CompanyListsSpec(BaseModel):
    """Where a curated watchlist comes from. One list at a time, by design: a second
    would need a merge policy for the same company appearing in both."""

    notion: NotionListSpec = Field(default_factory=NotionListSpec)
    csv: CsvListSpec = Field(default_factory=CsvListSpec)

    def configured(self) -> str | None:
        """The list source to use when `--from` is not given. Notion wins if both are
        set, since a CSV is usually the one-off export."""
        if self.notion.database_id:
            return "notion"
        if self.csv.path:
            return "csv"
        return None


class DedupeSpec(BaseModel):
    """When a new posting is the same job as one the user already dismissed."""

    # Cosine similarity of the posting text, within one company. Reworded reposts of the
    # same role score well above this; a different role at the same company does not.
    repost_similarity: float = Field(default=0.90, gt=0, le=1)


class Config(BaseModel):
    """Root config. Profile is a directory path, never a name in code."""

    db_path: str = "data/jobs.db"
    profile_dir: str = "profile"
    stop_after: int = 15
    # Used instead of stop_after while the jobs table is still empty.
    initial_target: int = 50
    # What one board may contribute to a run. A bound on the source, not on the day's
    # target: `stop_after` decides when a run has enough, this decides where it came from.
    max_jobs_per_target: int = Field(default=100, ge=1)
    inactive_after_days: int = 21
    scoring: ScoringSpec = Field(default_factory=ScoringSpec)
    filters: FilterSpec = Field(default_factory=FilterSpec)
    pii: PiiSpec = Field(default_factory=PiiSpec)
    observability: ObservabilitySpec = Field(default_factory=ObservabilitySpec)
    lifecycle: LifecycleSpec = Field(default_factory=LifecycleSpec)
    dedupe: DedupeSpec = Field(default_factory=DedupeSpec)
    models: ModelsSpec = Field(default_factory=ModelsSpec)
    discovery: DiscoverySpec = Field(default_factory=DiscoverySpec)
    company_lists: CompanyListsSpec = Field(default_factory=CompanyListsSpec)
    notion: NotionSpec = Field(default_factory=NotionSpec)
    targets: list[TargetSpec] = Field(default_factory=list)
    # Which model profile was overlaid on `models:`, and why. Set by `load`, never read
    # from YAML.
    profile: ProfileChoice = Field(default=NO_PROFILE, exclude=True)
    # What `load` read before any profile was overlaid, where the profiles live, and the
    # hardware probe (cached after its first answer). `resolve_models` re-runs the profile
    # choice from these when saved model settings decide it.
    _raw: dict[str, Any] | None = PrivateAttr(default=None)
    _profiles_dir: Path | None = PrivateAttr(default=None)
    _hardware: Callable[[], Hardware] | None = PrivateAttr(default=None)

    @classmethod
    def load(
        cls,
        path: str | Path = "config.yaml",
        *,
        hardware: Callable[[], Hardware] | None = None,
    ) -> Config:
        file = Path(path)
        if not file.exists():
            return cls()
        data = yaml.safe_load(file.read_text()) or {}
        data.pop("profile", None)  # the outcome below, never an input
        raw = copy.deepcopy(data)
        models = data.get("models") or {}
        profiles_dir = file.parent / "deploy" / "profiles"
        detect = _cached(hardware or detect_hardware)
        choice = choose_profile(
            os.environ,
            str(models.get("profile") or "auto"),
            detect,
            available_profiles(profiles_dir),
        )
        if choice.name:
            data = apply_profile(data, load_profile(profiles_dir / f"{choice.name}.yaml"))
        config = cls.model_validate(data)
        config.profile = choice
        config._raw = raw
        config._profiles_dir = profiles_dir
        config._hardware = detect
        return config

    def role(
        self,
        name: Literal["triage", "match", "discovery", "profiler", "curator", "assistant"],
    ) -> ResolvedRole:
        """Resolve one role against its backend, falling back to the `scoring:` ids.

        The fallback parses `openrouter/google/gemini-2.5-flash` back into a backend and
        a model, so the same `ResolvedRole` describes a configured local role and a
        legacy remote one. Every consumer sees one shape.
        """
        scoring = self.scoring
        if (
            name in ("discovery", "profiler", "curator", "assistant")
            and name not in self.models.roles
        ):
            # Board discovery, profiling and curation are cheap-judgement tasks, so they
            # share the triage model unless given one of their own. A third tier would be
            # a knob, not a capability — but the model must be able to call tools, which
            # `doctor` says.
            return replace(self.role("triage"), name=name)
        legacy_model = scoring.triage_model if name == "triage" else scoring.match_model
        legacy_tokens = (
            scoring.triage_max_output_tokens
            if name == "triage"
            else scoring.match_max_output_tokens
        )
        legacy_reserve = (
            scoring.triage_reserve_eur if name == "triage" else scoring.match_reserve_eur
        )
        spec = self.models.roles.get(name)
        if spec is None:
            kind, _, model = legacy_model.partition("/")
            if kind not in _LITELLM_PREFIX:
                kind, model = "openrouter", legacy_model
            backend = self.models.backends.get(kind, _DEFAULT_BACKENDS.get(kind))
            if backend is None:
                backend = BackendSpec(kind="openrouter")
            return ResolvedRole(
                name=name,
                kind=backend.kind,
                model=model,
                litellm_model=legacy_model,
                base_url=backend.base_url,
                max_output_tokens=legacy_tokens,
                reserve_eur=legacy_reserve,
                key_env=backend.key_env(),
                max_concurrency=backend.concurrency(),
            )
        backend = self.models.backends.get(spec.backend) or _DEFAULT_BACKENDS.get(spec.backend)
        if backend is None:
            raise ValueError(
                f"models.roles.{name}.backend '{spec.backend}' is not in models.backends"
            )
        return ResolvedRole(
            name=name,
            kind=backend.kind,
            model=spec.model,
            litellm_model=f"{_LITELLM_PREFIX[backend.kind]}/{spec.model}",
            base_url=backend.base_url,
            max_output_tokens=spec.max_output_tokens,
            reserve_eur=legacy_reserve if spec.reserve_eur is None else spec.reserve_eur,
            key_env=backend.key_env(),
            max_concurrency=backend.concurrency(),
        )

    def backend(self, name: str) -> BackendSpec | None:
        """A backend by name, including the three built in (`ollama`, `vllm`, `openrouter`)."""
        return self.models.backends.get(name) or _DEFAULT_BACKENDS.get(name)

    def source_targets(self) -> list[SourceTarget]:
        return [spec.to_target() for spec in self.targets]


def _cached(hardware: Callable[[], Hardware]) -> Callable[[], Hardware]:
    """Probe the machine at most once per loaded config: `nvidia-smi` may take seconds,
    and settings are resolved again at every run start."""
    found: list[Hardware] = []

    def detect() -> Hardware:
        if not found:
            found.append(_safe_detect(hardware))
        return found[0]

    return detect


# Token limits for a role an override adds where the profile had none, as the profiles set.
_OVERRIDE_TOKENS = {"triage": 256}


def resolve_models(
    config: Config, settings: ModelSettings | None, env: Mapping[str, str]
) -> Config:
    """The config a run should use, once the user's saved model settings are applied.

    Precedence (spec 014): env `CAREER_COPILOT_PROFILE`, then the saved preset, then
    `models.profile`, then auto. Saved per-role overrides apply last, over whichever won.
    With nothing saved, the loaded config is returned as it is.
    """
    if settings is None or (settings.preset is None and not settings.overrides):
        return config
    base: dict[str, Any] = (
        copy.deepcopy(config._raw) if config._raw is not None else config.model_dump()
    )
    profiles_dir = config._profiles_dir or Path("deploy") / "profiles"
    available = available_profiles(profiles_dir)
    detect = config._hardware or _cached(detect_hardware)
    choice = config.profile

    def overlay(name: str) -> dict[str, Any]:
        if name not in available:
            raise ProfileError(f"model profile '{name}' is missing from {profiles_dir}")
        return load_profile(profiles_dir / f"{name}.yaml")

    data = base
    if settings.preset is not None and not env.get(ENV_VAR, "").strip():
        configured = str((base.get("models") or {}).get("profile") or AUTO)
        if configured == CLOUD_PROFILE:
            configured = AUTO  # a local pick must never be the cloud profile
        local = choose_profile({}, configured, detect, available - {CLOUD_PROFILE})
        if local.name:
            data = apply_profile(base, overlay(local.name))
        if settings.preset == "cloud":
            data = apply_profile(base, overlay(CLOUD_PROFILE))
            choice = ProfileChoice(CLOUD_PROFILE, "settings", "settings: Cloud preset")
        elif settings.preset == "hybrid":
            cloud = overlay(CLOUD_PROFILE)["models"]
            data = apply_profile(
                data,
                {
                    "models": {
                        "backends": cloud.get("backends") or {},
                        "roles": {
                            role: spec
                            for role, spec in (cloud.get("roles") or {}).items()
                            if role in HYBRID_CLOUD_ROLES
                        },
                    }
                },
            )
            choice = ProfileChoice(
                local.name, "settings", f"settings: Hybrid preset, local triage ({local.reason})"
            )
        else:
            choice = ProfileChoice(
                local.name, "settings", f"settings: Local preset ({local.reason})"
            )
    elif choice.name:
        data = apply_profile(base, overlay(choice.name))
    if settings.overrides:
        models = data.setdefault("models", {}) or {}
        data["models"] = models
        roles = models.get("roles") or {}
        models["roles"] = roles
        for role, override in settings.overrides.items():
            current = roles.get(role) or {}
            spec: dict[str, Any] = {
                "backend": override.backend,
                "model": override.model,
                "max_output_tokens": current.get("max_output_tokens")
                or _OVERRIDE_TOKENS.get(role, 1024),
            }
            if override.reserve_eur is not None:
                spec["reserve_eur"] = override.reserve_eur
            roles[role] = spec
    # Only `models:` changes. Everything else is the loaded config's own, so an edit made to
    # it while the server runs (the company list) is never undone by a resolve.
    resolved = config.model_copy(
        update={"models": ModelsSpec.model_validate(data.get("models") or {})}
    )
    resolved.profile = choice
    return resolved


def _safe_detect(hardware: Callable[[], Hardware]) -> Hardware:
    """Detection is a guess about the machine; it must never stop start-up."""
    try:
        return hardware()
    except Exception:
        return Hardware("other", "")
