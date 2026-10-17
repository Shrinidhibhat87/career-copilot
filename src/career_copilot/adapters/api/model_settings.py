"""HTTP for choosing models and the OpenRouter key (spec 014).

The key is write-only over this API: it goes in, it is tested, it is stored in the OS
keychain, and the most that comes back is its last four characters. Settings rows never
hold it.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Query

from career_copilot.adapters.llm.openrouter import OpenRouterDirectory, OpenRouterUnavailable
from career_copilot.adapters.model_catalog import load_catalog
from career_copilot.adapters.profiles import ENV_VAR, ProfileError
from career_copilot.adapters.secrets import OPENROUTER, ChainedSecretStore, default_secrets, tail
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, resolve_models
from career_copilot.domain.model_settings import (
    OPENROUTER as OPENROUTER_KIND,
)
from career_copilot.domain.model_settings import (
    ROLE_CAPS,
    ROLES,
    LiveModel,
    ModelSettings,
    ModelSettingsError,
    RoleOverride,
    gate_cloud_model,
    missing_cap,
    parse_overrides,
    parse_preset,
)
from career_copilot.ports import SecretStoreUnavailable

_DIRECTORY = OpenRouterDirectory()


def _error(status: int, field: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"errors": {field: message}})


def router(
    get_config: Callable[[], Config],
    secrets: Callable[[], ChainedSecretStore] = default_secrets,
    directory: OpenRouterDirectory = _DIRECTORY,
    env: Mapping[str, str] | None = None,
) -> APIRouter:
    routes = APIRouter(prefix="/api/settings", tags=["settings"])

    def environ() -> Mapping[str, str]:
        return os.environ if env is None else env

    def key_status() -> dict[str, object]:
        store = secrets()
        value, source = store.get_with_source(OPENROUTER)
        return {
            "set": value is not None,
            "source": source,
            "tail": tail(value),
            "keychain": store.writable(),
        }

    def saved(config: Config) -> ModelSettings | None:
        with SqliteStore(config.db_path) as store:
            return store.model_settings()

    def payload(config: Config, settings: ModelSettings | None) -> dict[str, object]:
        resolved = resolve_models(config, settings, environ())
        overrides = settings.overrides if settings else {}
        preset_decided = resolved.profile.source == "settings"
        roles = []
        for name in ROLES:
            role = resolved.role(name)  # type: ignore[arg-type]
            source = "override" if name in overrides else "preset" if preset_decided else "config"
            roles.append(
                {
                    "role": name,
                    "kind": role.kind,
                    "model": role.model,
                    "source": source,
                    "local": role.is_local,
                    "needs": ROLE_CAPS[name],
                }
            )
        with SqliteStore(config.db_path) as store:
            last = store.list_runs(1)
        return {
            "settings": {
                "version": settings.version if settings else 0,
                "preset": settings.preset if settings else None,
                "overrides": {
                    role: {
                        "backend": o.backend,
                        "model": o.model,
                        "reserveEur": o.reserve_eur,
                    }
                    for role, o in overrides.items()
                },
                "cloudConsent": bool(settings and settings.cloud_consent),
                "savedAt": (
                    settings.saved_at.isoformat() if settings and settings.saved_at else None
                ),
            },
            "profile": {
                "name": resolved.profile.name,
                "source": resolved.profile.source,
                "reason": resolved.profile.reason,
            },
            # Set in a dev shell, it beats any saved preset; the page says so.
            "envProfile": environ().get(ENV_VAR, "").strip() or None,
            "roles": roles,
            "backends": sorted({*config.models.backends, "ollama", "vllm", OPENROUTER_KIND}),
            "key": key_status(),
            "budget": {
                "maxEurPerRun": config.scoring.max_eur_per_run,
                "lastRunEur": last[0]["cost_eur"] if last else None,
            },
        }

    @routes.get("/models")
    def read_models() -> dict[str, object]:
        config = get_config()
        return payload(config, saved(config))

    @routes.put("/models")
    def write_models(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        config = get_config()
        try:
            preset = parse_preset(body.get("preset"))
            overrides = parse_overrides(body.get("overrides"))
        except ModelSettingsError as exc:
            raise _error(422, "settings", str(exc)) from None
        live: dict[str, LiveModel] | None = None
        gated: dict[str, RoleOverride] = {}
        catalog = load_catalog()
        for role, override in overrides.items():
            backend = config.backend(override.backend)
            if backend is None:
                raise _error(422, role, f"unknown backend '{override.backend}'")
            if backend.kind != OPENROUTER_KIND:
                gated[role] = override
                continue
            if live is None:
                try:
                    live = directory.models()
                except OpenRouterUnavailable:
                    live = {}  # fall back to what the catalog knows
            if live and override.model not in live:
                raise _error(422, role, f"{override.model} is not on OpenRouter's model list")
            problem, reserve = gate_cloud_model(
                role, override.model, live.get(override.model), catalog, override.reserve_eur
            )
            if problem:
                raise _error(422, role, problem)
            gated[role] = RoleOverride(override.backend, override.model, reserve)
        prior = saved(config)
        consent = body.get("cloudConsent") is True or bool(prior and prior.cloud_consent)
        candidate = ModelSettings(preset=preset, overrides=gated, cloud_consent=consent)
        try:
            resolved = resolve_models(config, candidate, environ())
            cloud = [
                name
                for name in ROLES
                if resolved.role(name).kind == OPENROUTER_KIND  # type: ignore[arg-type]
            ]
        except (ProfileError, ValueError) as exc:
            raise _error(422, "settings", str(exc)) from None
        if cloud and not secrets().get(OPENROUTER):
            raise _error(409, "key", "Add an OpenRouter key before moving a role to the cloud.")
        if cloud and not consent:
            raise _error(
                428,
                "consent",
                "Accept the cloud notice first: the CV and jobs leave this machine, redacted.",
            )
        with SqliteStore(config.db_path) as store:
            stored = store.save_model_settings(candidate, datetime.now(UTC))
        return payload(config, stored)

    @routes.get("/openrouter-key")
    def read_key() -> dict[str, object]:
        return key_status()

    def check(key: str) -> dict[str, object]:
        try:
            result = directory.check_key(key)
        except OpenRouterUnavailable as exc:
            raise _error(502, "key", str(exc)) from None
        return {
            "valid": result.valid,
            "limit": result.limit,
            "usage": result.usage,
            "detail": result.detail,
        }

    @routes.post("/openrouter-key/test")
    def test_key(body: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, object]:
        raw = (body or {}).get("key")
        key = raw.strip() if isinstance(raw, str) and raw.strip() else secrets().get(OPENROUTER)
        if not key:
            raise _error(422, "key", "No key to test: paste one, or save one first.")
        return check(key)

    @routes.put("/openrouter-key")
    def write_key(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
        raw = body.get("key")
        key = raw.strip() if isinstance(raw, str) else ""
        if not key:
            raise _error(422, "key", "Paste a key first.")
        if not check(key)["valid"]:
            raise _error(422, "key", "OpenRouter rejected this key. It was not saved.")
        # The keychain, or this server session when the machine has none (WSL).
        secrets().save(OPENROUTER, key)
        return key_status()

    @routes.delete("/openrouter-key")
    def clear_key() -> dict[str, object]:
        # The keychain entry only. A key in .env is the developer's, and stays.
        try:
            secrets().delete(OPENROUTER)
        except SecretStoreUnavailable as exc:
            raise _error(503, "key", str(exc)) from None
        return key_status()

    @routes.get("/openrouter-models")
    def list_models(role: str = Query(...)) -> dict[str, object]:
        if role not in ROLES:
            raise _error(422, "role", f"unknown role '{role}'")
        try:
            live: dict[str, LiveModel] = directory.models()
            live_error = ""
        except OpenRouterUnavailable as exc:
            live, live_error = {}, str(exc)
        entries = load_catalog().for_role(role)
        listed = {entry.id for entry in entries}

        def row(model: LiveModel) -> dict[str, object]:
            return {
                "id": model.id,
                "name": model.name,
                "priceIn": model.price_in,
                "priceOut": model.price_out,
                "dynamic": model.dynamic,
            }

        catalog_rows = []
        for entry in entries:
            found = live.get(entry.id)
            catalog_rows.append(
                {
                    "id": entry.id,
                    "tier": entry.tier,
                    "note": entry.note,
                    "reserveEur": entry.reserve_eur,
                    "priceIn": found.price_in if found else None,
                    "priceOut": found.price_out if found else None,
                    "dynamic": found.dynamic if found else False,
                    # Greyed out in the picker; unknown while the live list is unreachable.
                    "available": found is not None if live else None,
                }
            )
        others = [
            row(model)
            for model in sorted(live.values(), key=lambda m: m.id)
            if model.id not in listed and missing_cap(role, model.caps) is None
        ]
        return {"catalog": catalog_rows, "models": others, "liveError": live_error}

    return routes
