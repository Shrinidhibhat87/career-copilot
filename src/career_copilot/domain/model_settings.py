"""Which model serves each role, as the user chose it (spec 014).

A preset names a model profile; an override replaces one role. Neither ever holds a key:
the key lives in the OS keychain or `.env`, and this module never sees it. The catalog is
the curated list a role's picker shows first; it is data, so the model eval changes it,
not code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, cast

Preset = Literal["local", "cloud", "hybrid"]
Tier = Literal["recommended", "challenger", "baseline"]
Cap = Literal["tools", "response_format", "structured_outputs"]

PRESETS: tuple[Preset, ...] = ("local", "cloud", "hybrid")
TIERS: tuple[Tier, ...] = ("recommended", "challenger", "baseline")
CAPS: tuple[Cap, ...] = ("tools", "response_format", "structured_outputs")
ROLES = ("triage", "match", "profiler", "curator", "assistant", "discovery")

# What a role's model must support on OpenRouter. The agents call tools; the scoring roles
# and the profiler answer in JSON.
ROLE_CAPS: Mapping[str, Cap] = {
    "triage": "response_format",
    "match": "response_format",
    "profiler": "response_format",
    "curator": "tools",
    "assistant": "tools",
    "discovery": "tools",
}

# Hybrid moves these to the cloud. Triage, the high-volume cheap pass, stays local.
HYBRID_CLOUD_ROLES = ("match", "assistant", "profiler", "curator", "discovery")

OPENROUTER = "openrouter"


class ModelSettingsError(ValueError):
    """A settings payload or catalog entry that cannot be used. The message names why."""


@dataclass(frozen=True, slots=True)
class RoleOverride:
    backend: str  # a name in models.backends, or "openrouter"
    model: str
    # Set when the live price is dynamic: the meter needs a worst case to reserve.
    reserve_eur: float | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"backend": self.backend, "model": self.model}
        if self.reserve_eur is not None:
            payload["reserve_eur"] = self.reserve_eur
        return payload


@dataclass(frozen=True, slots=True)
class ModelSettings:
    version: int = 0
    preset: Preset | None = None  # None: no saved choice, fall through to config/auto
    overrides: Mapping[str, RoleOverride] = field(default_factory=dict)
    cloud_consent: bool = False
    saved_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    id: str  # OpenRouter model id
    roles: tuple[str, ...]
    tier: Tier
    caps: tuple[Cap, ...]
    note: str
    reserve_eur: float | None = None  # required when the live price is dynamic


@dataclass(frozen=True, slots=True)
class LiveModel:
    """One row of OpenRouter's model list, reduced to what a picker and the gate need.

    Prices are per million tokens. `dynamic` marks a model whose price is only known after
    the call (OpenRouter lists -1), so nothing can be reserved against it upfront.
    """

    id: str
    name: str
    caps: frozenset[str]
    price_in: float | None
    price_out: float | None
    dynamic: bool = False


def parse_preset(value: object) -> Preset | None:
    if value is None:
        return None
    if value not in PRESETS:
        raise ModelSettingsError(f"preset must be one of {', '.join(PRESETS)}")
    return value


def parse_overrides(raw: object) -> dict[str, RoleOverride]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ModelSettingsError("overrides must be an object keyed by role")
    overrides: dict[str, RoleOverride] = {}
    for role, value in raw.items():
        if role not in ROLES:
            raise ModelSettingsError(f"unknown role '{role}'")
        if not isinstance(value, Mapping):
            raise ModelSettingsError(f"override for {role} must be an object")
        backend = str(value.get("backend") or "").strip()
        model = str(value.get("model") or "").strip()
        if not backend or not model:
            raise ModelSettingsError(f"override for {role} needs a backend and a model")
        reserve = value.get("reserve_eur", value.get("reserveEur"))
        if reserve is not None and (
            isinstance(reserve, bool) or not isinstance(reserve, int | float) or reserve <= 0
        ):
            raise ModelSettingsError(f"reserve for {role} must be a positive number")
        overrides[str(role)] = RoleOverride(
            backend, model, float(reserve) if reserve is not None else None
        )
    return overrides


def missing_cap(role: str, caps: Iterable[str]) -> Cap | None:
    """The parameter a model lacks for this role, or None when it passes the gate."""
    needed = ROLE_CAPS.get(role)
    return needed if needed is not None and needed not in set(caps) else None


@dataclass(frozen=True, slots=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]

    def for_role(self, role: str) -> list[CatalogEntry]:
        """The role's entries, best tier first. A model listed twice for a role (the
        profiler's recommended pick is also its baseline) appears once, at its best tier."""
        order = {tier: index for index, tier in enumerate(TIERS)}
        picked: dict[str, CatalogEntry] = {}
        for entry in sorted(self.entries, key=lambda entry: order[entry.tier]):
            if role in entry.roles:
                picked.setdefault(entry.id, entry)
        return list(picked.values())

    def recommended(self, role: str) -> CatalogEntry:
        for entry in self.entries:
            if role in entry.roles and entry.tier == "recommended":
                return entry
        raise ModelSettingsError(f"the catalog has no recommended model for {role}")

    def entry(self, model_id: str) -> CatalogEntry | None:
        return next((entry for entry in self.entries if entry.id == model_id), None)

    def problems(self) -> list[str]:
        """What is wrong with the catalog itself. Empty for a shippable catalog."""
        found: list[str] = []
        for role in ROLES:
            tiers = [entry.tier for entry in self.entries if role in entry.roles]
            if tiers.count("recommended") != 1:
                found.append(
                    f"{role}: expected one recommended entry, found {tiers.count('recommended')}"
                )
            if "baseline" not in tiers:
                found.append(f"{role}: no baseline entry")
        for entry in self.entries:
            for role in entry.roles:
                cap = missing_cap(role, entry.caps)
                if cap is not None:
                    found.append(f"{entry.id}: listed for {role} but lacks {cap}")
        return found


def parse_catalog(data: object) -> Catalog:
    if not isinstance(data, Mapping) or not isinstance(data.get("models"), list):
        raise ModelSettingsError("the model catalog must hold a `models:` list")
    entries: list[CatalogEntry] = []
    for raw in data["models"]:
        if not isinstance(raw, Mapping):
            raise ModelSettingsError("each catalog entry must be an object")
        model_id = str(raw.get("id") or "")
        roles = tuple(str(role) for role in raw.get("roles") or ())
        tier = raw.get("tier")
        caps = tuple(raw.get("caps") or ())
        unknown_roles = [role for role in roles if role not in ROLES]
        if not model_id or not roles or unknown_roles:
            raise ModelSettingsError(f"catalog entry '{model_id}' has bad roles {roles}")
        if tier not in TIERS:
            raise ModelSettingsError(f"catalog entry '{model_id}' has bad tier {tier!r}")
        if any(cap not in CAPS for cap in caps):
            raise ModelSettingsError(f"catalog entry '{model_id}' has bad caps {caps}")
        reserve = raw.get("reserve_eur")
        entries.append(
            CatalogEntry(
                id=model_id,
                roles=roles,
                tier=cast(Tier, tier),
                caps=cast(tuple[Cap, ...], caps),
                note=str(raw.get("note") or ""),
                reserve_eur=float(reserve) if reserve is not None else None,
            )
        )
    return Catalog(tuple(entries))


def gate_cloud_model(
    role: str,
    model_id: str,
    live: LiveModel | None,
    catalog: Catalog,
    reserve_eur: float | None,
) -> tuple[str | None, float | None]:
    """Check one OpenRouter model for one role before it is saved.

    Returns (problem, reserve): a problem refuses the save; the reserve is the worst case
    the meter should hold, taken from the override or else the catalog. The live list is
    preferred over the catalog's caps, which may be stale; with no live list, a catalog
    entry is trusted and anything else is refused.
    """
    entry = catalog.entry(model_id)
    reserve = reserve_eur if reserve_eur is not None else (entry.reserve_eur if entry else None)
    if live is not None:
        caps: Iterable[str] = live.caps
    elif entry is not None:
        caps = entry.caps
    else:
        return f"{model_id} is not on OpenRouter's model list", reserve
    cap = missing_cap(role, caps)
    if cap is not None:
        return f"{model_id} does not support {cap}, which {role} needs", reserve
    if live is not None and live.dynamic and reserve is None:
        return (
            f"{model_id} has no upfront price; give it a reserve in euros per call",
            reserve,
        )
    return None, reserve
