"""`career-copilot models ...` — which model serves each role, and the OpenRouter key.

Same store and secret port as the Settings page (spec 014), so either can make a change
and the other shows it.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

import typer
from dotenv import load_dotenv

from career_copilot.adapters import cli_user
from career_copilot.adapters.llm.openrouter import OpenRouterDirectory, OpenRouterUnavailable
from career_copilot.adapters.secrets import OPENROUTER, default_secrets, tail
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import resolve_models
from career_copilot.domain.model_settings import (
    ROLES,
    ModelSettings,
    ModelSettingsError,
    parse_preset,
)
from career_copilot.ports import SecretStoreUnavailable
from career_copilot.usecases.doctor import profile_line

app = typer.Typer(help="Choose where each role's model runs, and manage the OpenRouter key.")
key_app = typer.Typer(help="The OpenRouter key, kept in the OS keychain.")
app.add_typer(key_app, name="key")

CONSENT = "Cloud roles send your CV and jobs to OpenRouter, redacted. Continue?"


@app.command()
def show(config_path: Annotated[str, typer.Option("--config")] = "config.yaml") -> None:
    """Print the role map in force and where the key comes from."""
    load_dotenv()
    config = cli_user.load(config_path)
    with SqliteStore(config.db_path) as store:
        settings = store.model_settings()
    resolved = resolve_models(config, settings, os.environ)
    typer.echo(profile_line(resolved.profile))
    overrides = settings.overrides if settings else {}
    for name in ROLES:
        role = resolved.role(name)  # type: ignore[arg-type]
        marker = " (override)" if name in overrides else ""
        typer.echo(f"  {name:<9} {role.kind}:{role.model}{marker}")
    value, source = default_secrets().get_with_source(OPENROUTER)
    typer.echo(f"OpenRouter key: {f'…{tail(value)} from {source}' if value else 'not set'}")


@app.command()
def preset(
    name: Annotated[str, typer.Argument(help="local, cloud or hybrid")],
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    yes: Annotated[bool, typer.Option("--yes", help="Accept the cloud notice")] = False,
) -> None:
    """Save a preset. Per-role overrides are kept."""
    load_dotenv()
    try:
        chosen = parse_preset(name)
    except ModelSettingsError as exc:
        raise typer.BadParameter(str(exc)) from None
    config = cli_user.load(config_path)
    with SqliteStore(config.db_path) as store:
        prior = store.model_settings() or ModelSettings()
        candidate = replace(prior, preset=chosen)
        resolved = resolve_models(config, candidate, os.environ)
        cloud = [n for n in ROLES if resolved.role(n).kind == OPENROUTER]  # type: ignore[arg-type]
        if cloud and not default_secrets().get(OPENROUTER):
            typer.secho("No OpenRouter key. Run: career-copilot models key set", fg="red")
            raise typer.Exit(code=1)
        if cloud and not candidate.cloud_consent:
            if not (yes or typer.confirm(CONSENT)):
                raise typer.Exit(code=1)
            candidate = replace(candidate, cloud_consent=True)
        store.save_model_settings(candidate, datetime.now(UTC))
    typer.echo(f"Saved preset {chosen}. Applies from the next run.")


@key_app.command("set")
def key_set() -> None:
    """Paste a key (hidden). It is tested with OpenRouter before it is stored."""
    key = typer.prompt("OpenRouter key", hide_input=True).strip()
    try:
        check = OpenRouterDirectory().check_key(key)
    except OpenRouterUnavailable as exc:
        typer.secho(f"Not saved: {exc}", fg="red")
        raise typer.Exit(code=1) from None
    if not check.valid:
        typer.secho("Not saved: OpenRouter rejected this key.", fg="red")
        raise typer.Exit(code=1)
    store = default_secrets()
    if not store.writable():
        # A CLI process ends right away, so a session-only key would be useless here.
        typer.secho("Not saved: no OS keychain; put OPENROUTER_API_KEY in .env", fg="red")
        raise typer.Exit(code=1)
    store.save(OPENROUTER, key)
    typer.echo(f"Saved …{tail(key)} to the OS keychain.")


@key_app.command("clear")
def key_clear() -> None:
    """Remove the key from the OS keychain. A key in .env is left alone."""
    try:
        default_secrets().delete(OPENROUTER)
    except SecretStoreUnavailable as exc:
        typer.secho(str(exc), fg="red")
        raise typer.Exit(code=1) from None
    typer.echo("Cleared the keychain entry.")
