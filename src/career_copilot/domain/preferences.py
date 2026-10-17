"""The preference model. Pure: no I/O, no store, no config file.

One structured model behind both halves of the pipeline. `to_filter_config()` feeds the
deterministic rules in `domain/filters.py`; `to_prompt_block()` renders the `PREFERENCES`
block both agents embed. Before this existed the same facts lived twice — as prose in
`profile/preferences.md` and as `filters:` in `config.yaml` — and could drift apart.

Every term tuple holds lowercase *match terms*, never display labels: a term is only ever
fed to `filters.term_pattern`, and a label like `LLM / RAG` matches no posting text.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Literal, get_args

from career_copilot.domain.filters import FilterConfig

CefrLevel = Literal["A1", "A2", "B1", "B2", "C1", "C2", "native"]
SeniorityBand = Literal["junior", "mid", "senior", "staff", "lead"]

CEFR_LEVELS: tuple[str, ...] = get_args(CefrLevel)
SENIORITY_BANDS: tuple[str, ...] = get_args(SeniorityBand)

# A role family is prompt material, not a filter input, so five is a product limit on how
# much the prompt claims to be looking for rather than a technical bound.
MAX_ROLE_FAMILIES = 5

# The free-text answer to "what are you looking for?", in the user's own words. A ceiling
# rather than a shape: it is prompt material, and an unbounded field would be paid for on
# every scoring call.
MAX_ROLE_INTENT_CHARS = 600

# The positive role gate's seed. Separate from `role_families` because the two are
# different kinds of value: a family is a label the model reads (`ai/ml engineering`),
# a title term is a matcher a posting title is tested against, and the label matches no
# posting text that has ever been published.
DEFAULT_TITLE_TERMS: tuple[str, ...] = FilterConfig().title_terms


class PreferencesError(ValueError):
    """Invalid preferences. Carries one message per offending field."""

    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__("; ".join(f"{field}: {message}" for field, message in errors.items()))
        self.errors = errors


@dataclass(frozen=True, slots=True)
class LanguageSkill:
    """A language the user has, at the level they have it.

    The level is what the user *holds*. The reject threshold is derived from it inside
    `domain/filters.py`, so raising a level here can only ever widen the search.
    """

    name: str
    level: CefrLevel

    @property
    def key(self) -> str:
        return self.name.strip().lower()


@dataclass(frozen=True, slots=True)
class Preferences:
    """Everything that drives a run. The source of truth for filters and prompts alike."""

    # What the user says they are looking for, in their own words. Prompt material only —
    # no rule reads it. `role_families` remains the structured form, and an agent deriving
    # one from the other is a later task, which is why both exist rather than one replacing
    # the other.
    role_intent: str = ""
    # The profiler's reading of the intent log, sample roles and CV. Attached per run,
    # never saved with the form; when present it replaces `role_intent` in the prompt.
    search_profile: str = ""
    role_families: tuple[str, ...] = ()
    # Matched against a posting's title. An empty tuple is a deliberate 'no gate'.
    title_terms: tuple[str, ...] = ()
    core_skills: tuple[str, ...] = ()
    excluded_stack: tuple[str, ...] = ()
    excluded_role_terms: tuple[str, ...] = ()
    seniority: tuple[SeniorityBand, ...] = ("mid", "senior")
    allowed_cities: tuple[str, ...] = ()
    preferred_cities: tuple[str, ...] = ()
    flagged_cities: tuple[str, ...] = ()
    allow_remote: bool = True
    require_permanent: bool = True
    languages: tuple[LanguageSkill, ...] = ()
    # Operational, not prompt material: both sit outside `prompt_hash()` by construction,
    # because `to_prompt_block()` never renders them.
    max_per_company: int = 2
    batch_size: int = 6
    notes: str = ""
    # 0 means "derived from config.yaml, never edited". Only a save assigns a real version.
    version: int = 0
    updated_at: datetime | None = None

    # --- derivations ------------------------------------------------------

    def to_filter_config(self) -> FilterConfig:
        """The deterministic rules' whole input. Previously `FilterSpec.to_filter_config`."""
        return FilterConfig(
            allowed_cities=frozenset(self.allowed_cities),
            preferred_cities=frozenset(self.preferred_cities),
            flagged_cities=frozenset(self.flagged_cities),
            allow_remote=self.allow_remote,
            require_permanent=self.require_permanent,
            excluded_role_terms=tuple(self.excluded_role_terms),
            title_terms=tuple(self.title_terms),
            core_stack_terms=tuple(self.core_skills),
            excluded_stack_terms=tuple(self.excluded_stack),
            max_per_company=self.max_per_company,
            seniority_bands=tuple(self.seniority),
            language_levels=tuple((language.key, language.level) for language in self.languages),
        )

    def to_prompt_block(self) -> str:
        """The `PREFERENCES` block both agents embed, notes included verbatim.

        Deterministic: the same preferences render the same bytes, which is what makes
        `prompt_hash()` a usable part of a score's identity.

        `title_terms` is deliberately absent. It is a matcher list, and `role_families`
        already tells the model what the user is looking for; rendering both would put a
        list of near-synonyms in every prompt and make a filter-tuning edit invalidate
        every score that was already paid for.
        """
        lines: list[str] = []
        looking_for = self.search_profile.strip() or self.role_intent.strip()
        if looking_for:
            lines += ["Looking for:", looking_for, ""]
        for heading, values in (
            ("Role families", self.role_families),
            ("Core skills", self.core_skills),
            ("Excluded stack", self.excluded_stack),
            ("Excluded role terms", self.excluded_role_terms),
            ("Seniority", self.seniority),
            ("Cities", self.allowed_cities),
            ("Preferred cities", self.preferred_cities),
            ("Flagged cities", self.flagged_cities),
        ):
            lines.append(f"{heading}: {', '.join(values) if values else 'none stated'}")
        lines.append(f"Remote: {'allowed' if self.allow_remote else 'not allowed'}")
        lines.append(
            f"Employment: {'permanent only' if self.require_permanent else 'any employment type'}"
        )
        languages = ", ".join(f"{lang.name} {lang.level}" for lang in self.languages)
        lines.append(f"Languages: {languages or 'none stated'}")
        if self.notes.strip():
            lines += ["", "Notes:", self.notes]
        return "\n".join(lines) + "\n"

    def prompt_hash(self) -> str:
        """Identity of the preferences *as the model sees them*.

        Hashing the rendered block rather than a hand-listed set of fields is what keeps
        `max_per_company`, `batch_size`, `version` and `updated_at` outside the hash
        permanently: a field only enters the hash by being rendered into the prompt.
        """
        return hashlib.sha256(self.to_prompt_block().encode()).hexdigest()[:16]

    def language_level(self, name: str) -> CefrLevel | None:
        for language in self.languages:
            if language.key == name.strip().lower():
                return language.level
        return None

    # --- serialization ----------------------------------------------------

    def to_payload(self) -> dict[str, Any]:
        """JSON-ready dict. Pure data: `version` and `updated_at` belong to the store."""
        return {
            "role_intent": self.role_intent,
            "search_profile": self.search_profile,
            "role_families": list(self.role_families),
            "title_terms": list(self.title_terms),
            "core_skills": list(self.core_skills),
            "excluded_stack": list(self.excluded_stack),
            "excluded_role_terms": list(self.excluded_role_terms),
            "seniority": list(self.seniority),
            "allowed_cities": list(self.allowed_cities),
            "preferred_cities": list(self.preferred_cities),
            "flagged_cities": list(self.flagged_cities),
            "allow_remote": self.allow_remote,
            "require_permanent": self.require_permanent,
            "languages": [{"name": lang.name, "level": lang.level} for lang in self.languages],
            "max_per_company": self.max_per_company,
            "batch_size": self.batch_size,
            "notes": self.notes,
        }

    @classmethod
    def from_payload(
        cls, payload: dict[str, Any], version: int = 0, updated_at: datetime | None = None
    ) -> Preferences:
        """Rebuild from a stored payload. Unknown keys are ignored, so an older row
        written before a field existed still loads, at that field's default."""

        def terms(key: str) -> tuple[str, ...]:
            value = payload.get(key, ())
            return tuple(str(item) for item in value) if isinstance(value, list) else ()

        languages = payload.get("languages", [])
        return cls(
            role_intent=str(payload.get("role_intent", "")),
            search_profile=str(payload.get("search_profile", "")),
            role_families=terms("role_families"),
            # Absent and empty mean different things here. A row written before the
            # role gate existed never stated a title list, and reading that as "no
            # gate" would leave every install saved before 2026-09-06 unfiltered by
            # title forever. A list saved *as* empty is the user turning the gate off.
            title_terms=(terms("title_terms") if "title_terms" in payload else DEFAULT_TITLE_TERMS),
            core_skills=terms("core_skills"),
            excluded_stack=terms("excluded_stack"),
            excluded_role_terms=terms("excluded_role_terms"),
            seniority=tuple(terms("seniority")),  # type: ignore[arg-type]
            allowed_cities=terms("allowed_cities"),
            preferred_cities=terms("preferred_cities"),
            flagged_cities=terms("flagged_cities"),
            allow_remote=bool(payload.get("allow_remote", True)),
            require_permanent=bool(payload.get("require_permanent", True)),
            languages=tuple(
                LanguageSkill(name=str(item["name"]), level=item["level"])
                for item in languages
                if isinstance(item, dict) and "name" in item and "level" in item
            ),
            max_per_company=int(payload.get("max_per_company", 2)),
            batch_size=int(payload.get("batch_size", 6)),
            notes=str(payload.get("notes", "")),
            version=version,
            updated_at=updated_at,
        )


def normalize_term(value: str) -> str:
    """A hand-typed skill becomes a term. Any term is legal — it is only ever a matcher."""
    return " ".join(value.strip().lower().split())


def validate(prefs: Preferences) -> None:
    """Raise `PreferencesError` unless every field can drive a run.

    Shared by the save use case and the extraction proposal validator, so a proposal can
    never introduce a value a save would have refused.
    """
    errors: dict[str, str] = {}
    if len(prefs.role_families) > MAX_ROLE_FAMILIES:
        errors["role_families"] = f"choose at most {MAX_ROLE_FAMILIES} role families"
    if len(prefs.role_intent) > MAX_ROLE_INTENT_CHARS:
        errors["role_intent"] = f"keep this under {MAX_ROLE_INTENT_CHARS} characters"
    if not prefs.allowed_cities:
        errors["allowed_cities"] = "list at least one location"
    if not prefs.seniority:
        errors["seniority"] = "choose at least one seniority band"
    bad_bands = [band for band in prefs.seniority if band not in SENIORITY_BANDS]
    if bad_bands:
        errors["seniority"] = f"unknown seniority band: {', '.join(sorted(bad_bands))}"
    bad_levels = [lang.name for lang in prefs.languages if lang.level not in CEFR_LEVELS]
    if bad_levels:
        errors["languages"] = f"unknown level for: {', '.join(sorted(bad_levels))}"
    duplicates = {lang.key for lang in prefs.languages}
    if len(duplicates) != len(prefs.languages):
        errors["languages"] = "each language may appear once"
    if prefs.batch_size < 1:
        errors["batch_size"] = "batch size must be at least 1"
    if prefs.max_per_company < 1:
        errors["max_per_company"] = "max per company must be at least 1"
    if errors:
        raise PreferencesError(errors)


def bootstrap(filter_config: FilterConfig, notes: str, batch_size: int) -> Preferences:
    """Derive preferences from the `config.yaml` seed. Held in memory, never saved.

    `version = 0` is what marks these as "config seed, never edited": it is the flag the
    API reads to decide whether resume detections may still prefill the screen.
    """
    return replace(
        Preferences(
            title_terms=tuple(filter_config.title_terms),
            core_skills=tuple(filter_config.core_stack_terms),
            excluded_stack=tuple(filter_config.excluded_stack_terms),
            excluded_role_terms=tuple(filter_config.excluded_role_terms),
            seniority=tuple(filter_config.seniority_bands),  # type: ignore[arg-type]
            allowed_cities=tuple(sorted(filter_config.allowed_cities)),
            preferred_cities=tuple(sorted(filter_config.preferred_cities)),
            flagged_cities=tuple(sorted(filter_config.flagged_cities)),
            allow_remote=filter_config.allow_remote,
            require_permanent=filter_config.require_permanent,
            languages=tuple(
                LanguageSkill(name=name.title(), level=level)  # type: ignore[arg-type]
                for name, level in filter_config.language_levels
            ),
            max_per_company=filter_config.max_per_company,
            batch_size=batch_size,
            notes=notes,
        ),
        version=0,
    )


# --- proposed changes -----------------------------------------------------------------

FieldAction = Literal["add", "remove", "set"]

# Fields holding match terms. `add`/`remove` operate on one term at a time, which is what
# makes a proposal reviewable: the user accepts "add kafka", not "replace your skills".
TERM_FIELDS: tuple[str, ...] = (
    "role_families",
    "title_terms",
    "core_skills",
    "excluded_stack",
    "excluded_role_terms",
    "allowed_cities",
    "preferred_cities",
    "flagged_cities",
)
BOOL_FIELDS: tuple[str, ...] = ("allow_remote", "require_permanent")
INT_FIELDS: tuple[str, ...] = ("max_per_company", "batch_size")
CHANGEABLE_FIELDS: tuple[str, ...] = (
    *TERM_FIELDS,
    *BOOL_FIELDS,
    *INT_FIELDS,
    "seniority",
    "languages",
)


@dataclass(frozen=True, slots=True)
class FieldChange:
    """One proposed edit to one field. Never applied on its own: the user accepts it.

    `value` is always a string so the change survives JSON, the HTTP payload and the form
    unchanged. A language is `name:LEVEL`; a boolean is `true`/`false`; an integer is its
    digits.
    """

    field: str
    action: FieldAction
    value: str
    rationale: str = ""


def _language_from_value(value: str) -> LanguageSkill:
    name, _, level = value.partition(":")
    return LanguageSkill(name=name.strip(), level=level.strip())  # type: ignore[arg-type]


def _with(prefs: Preferences, field_name: str, value: Any) -> Preferences:
    """`dataclasses.replace` with a field name chosen at runtime. The name is checked
    against `CHANGEABLE_FIELDS` before this is reached, so it is never caller input."""
    updates: dict[str, Any] = {field_name: value}
    return replace(prefs, **updates)


def apply_change(prefs: Preferences, change: FieldChange) -> Preferences:
    """The preferences that would result from accepting this change. Pure.

    Raises `PreferencesError` when the change cannot be applied at all — an unknown field,
    an action the field does not support, or an unreadable value. A change that applies but
    produces invalid preferences is caught by `validate`, not here.
    """
    field_name, action, raw = change.field, change.action, change.value
    if field_name not in CHANGEABLE_FIELDS:
        raise PreferencesError({field_name: "unknown field"})
    if action not in ("add", "remove", "set"):
        raise PreferencesError({field_name: f"unknown action: {action}"})

    if field_name in BOOL_FIELDS or field_name in INT_FIELDS:
        if action != "set":
            raise PreferencesError({field_name: f"{field_name} takes set, not {action}"})
        if field_name in BOOL_FIELDS:
            if raw.strip().lower() not in ("true", "false"):
                raise PreferencesError({field_name: "expected true or false"})
            return _with(prefs, field_name, raw.strip().lower() == "true")
        try:
            number = int(raw.strip())
        except ValueError:
            raise PreferencesError({field_name: "expected a whole number"}) from None
        return _with(prefs, field_name, number)

    if field_name == "languages":
        language = _language_from_value(raw)
        if not language.name:
            raise PreferencesError({field_name: "expected name:LEVEL"})
        others = tuple(item for item in prefs.languages if item.key != language.key)
        if action == "remove":
            return replace(prefs, languages=others)
        if language.level not in CEFR_LEVELS:
            raise PreferencesError({field_name: f"unknown level: {language.level}"})
        # `add` on a language already listed is a level change, not a duplicate row.
        return replace(prefs, languages=(*others, language))

    values: tuple[str, ...] = getattr(prefs, field_name)
    term = normalize_term(raw) if field_name != "seniority" else raw.strip().lower()
    if not term:
        raise PreferencesError({field_name: "value is empty"})
    if action == "remove":
        updated = tuple(item for item in values if item != term)
    elif action == "add":
        updated = values if term in values else (*values, term)
    else:
        raise PreferencesError({field_name: f"{field_name} takes add or remove, not set"})
    return _with(prefs, field_name, updated)


def is_applicable(prefs: Preferences, change: FieldChange) -> bool:
    """Whether accepting this change would leave valid preferences.

    The same `validate` a save runs, so nothing a model proposes can reach the form that
    a save would then refuse.
    """
    try:
        validate(apply_change(prefs, change))
    except PreferencesError:
        return False
    return True
