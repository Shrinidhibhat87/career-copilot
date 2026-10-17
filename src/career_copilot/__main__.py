"""Driving adapter: the CLI."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv

from career_copilot.adapters import cli_user
from career_copilot.adapters.cli_models import app as models_app
from career_copilot.adapters.cli_sources import app as sources_app
from career_copilot.adapters.store.sqlite_store import SqliteStore, process_started_at
from career_copilot.config import Config, resolve_models
from career_copilot.domain.models import FilterOutcome
from career_copilot.domain.runs import RunInProgress
from career_copilot.usecases.compare_backends import compare as compare_exports
from career_copilot.usecases.compare_backends import export_top
from career_copilot.usecases.doctor import diagnose, profile_line, start_hint
from career_copilot.usecases.model_settings import in_force
from career_copilot.usecases.pipeline import (
    PipelineRun,
    build_digest_run,
    build_discover_run,
    build_score_run,
    current_preferences,
    finalize_error,
    freeze_config,
    preview_score,
    refresh_profile,
)
from career_copilot.usecases.pipeline import (
    config_seed as config_seed,
)

app = typer.Typer(add_completion=False, help="Personal job-search agent.")
app.add_typer(sources_app, name="sources")
app.add_typer(models_app, name="models")


def _now() -> datetime:
    return datetime.now(UTC)


def _load(config_path: str) -> Config:
    """`config.yaml` with the model settings saved on the Settings page applied."""
    return in_force(cli_user.load(config_path))


@app.callback()
def main(
    user: Annotated[str | None, typer.Option("--user", help="Account to work on.")] = None,
) -> None:
    cli_user.select(user)


# Step 1 -> claim the single run slot, freezing config/preferences for this run
def _begin(
    store: SqliteStore,
    config: Config,
    kind: str,
    *,
    initial: bool = False,
    budget: int | None = None,
) -> int:
    frozen = freeze_config(
        config, store, current_preferences(store, config), initial=initial, budget_override=budget
    )
    try:
        return store.begin_run(kind, frozen, _now(), os.getpid(), process_started_at(os.getpid()))
    except RunInProgress:
        # One run holds the slot at a time, browser or terminal. Say so; do not traceback.
        typer.secho(
            "a run is already active — wait for it to finish, or check GET /api/runs",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1) from None


# Step 2 -> fetch configured boards, hard-filter, and store; no model calls
@app.command()
def discover(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fetch configured boards, filter, and store. No model calls."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    config = _load(config_path)
    store = SqliteStore(config.db_path)
    try:
        run_id = _begin(store, config, "discover")
        try:
            report = build_discover_run(config, store, run_id).execute()
            store.finish_run(run_id, _now(), f"fetched={report.fetched}")
        except BaseException as exc:
            finalize_error(store, run_id, exc)
            raise
        store.mark_inactive_before(_now() - timedelta(days=config.inactive_after_days))
    finally:
        store.close()

    typer.echo(
        f"run {report.run_id}: fetched {report.fetched}, new {report.inserted}, "
        f"changed {report.changed}, qualified {report.qualified}, "
        f"rejected {report.rejected}, review {report.manual_review}"
    )
    for failure in report.failed_sources:
        typer.secho(f"  source failed: {failure}", fg=typer.colors.RED)


# Step 3 -> list stored jobs by filter outcome: pass, manual_review, or reject
@app.command("list")
def list_jobs(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    outcome: Annotated[str, typer.Option("--outcome")] = "pass",
    limit: Annotated[int, typer.Option("--limit")] = 20,
) -> None:
    """List stored jobs by filter outcome: pass, manual_review, or reject."""
    config = _load(config_path)
    store = SqliteStore(config.db_path)
    try:
        jobs = store.list_jobs(FilterOutcome(outcome), limit=limit)
    finally:
        store.close()

    if not jobs:
        typer.echo("no jobs")
        return
    for job in jobs:
        typer.echo(f"{job.posting.company} — {job.posting.title} [{job.posting.location or 'n/a'}]")
        typer.echo(f"  {job.posting.canonical_url}")
        for warning in job.filter_warnings:
            typer.secho(f"  warning: {warning}", fg=typer.colors.YELLOW)
        for reason in job.filter_reasons:
            typer.secho(f"  reason: {reason}", fg=typer.colors.RED)


# Step 4 -> triage every hard-filter survivor, then deep-score the top N
@app.command()
def score(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    initial: Annotated[bool, typer.Option("--initial")] = False,
    budget: Annotated[int | None, typer.Option("--budget")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Triage every hard-filter survivor, then deep-score the top N."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    config = _load(config_path)
    load_dotenv()
    if not dry_run:
        _require_backends(config)

    store = SqliteStore(config.db_path)
    try:
        if dry_run:
            preview = preview_score(
                config,
                store,
                current_preferences(store, config),
                initial=initial,
                budget_override=budget,
            )
            typer.echo(f"preview [{preview.mode}]: candidates {preview.candidates_now}")
            typer.echo(
                f"  estimated calls {preview.estimated_calls}; "
                f"reserved worst-case €{preview.reserved_eur:.4f}; "
                f"configured cost ceiling €{preview.ceiling_eur:.4f}"
            )
            return
        run_id = _begin(store, config, "score", initial=initial, budget=budget)
        try:
            frozen = refresh_profile(config, store, store.run_config(run_id))
            report = build_score_run(config, store, run_id, frozen).execute()
            store.finish_run(run_id, _now(), f"matched={report.matched}")
        except BaseException as exc:
            finalize_error(store, run_id, exc)
            row = store.get_run(run_id)
            assert row is not None
            typer.secho(f"run {run_id}: {row['failure_message']}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
    finally:
        store.close()

    typer.echo(
        f"run {report.run_id} [{report.mode}]: candidates {report.candidates}, "
        f"triaged {report.triaged}, matched {report.matched} "
        f"({report.above_threshold}/{report.target} above the bar), failed {report.failed}, "
        f"€{report.cost_eur:.4f}"
    )
    if report.above_threshold < report.target:
        typer.echo(
            f"  found {report.above_threshold} of {report.target} wanted matches; "
            "every candidate was scored or the budget ran out"
        )


# Export the current top matches to JSON, one file per machine, for `compare`
@app.command("export-top")
def export_top_cmd(
    out: Annotated[str, typer.Option("--out")],
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """Write the current top matches, with reasons and model, to a JSON file."""
    config = _load(config_path)
    with SqliteStore(config.db_path) as store:
        payload = export_top(store.match_scores(limit=None), _now(), limit)
    Path(out).write_text(json.dumps(payload, indent=2))
    typer.echo(f"{len(payload['jobs'])} jobs from {', '.join(payload['models']) or 'no model'}")


# Compare two exports: overlap, rank agreement, score drift
@app.command()
def compare(
    first: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    second: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
) -> None:
    """Compare two `export-top` files, e.g. vLLM on Windows against Ollama on the Mac."""
    a, b = json.loads(first.read_text()), json.loads(second.read_text())
    result = compare_exports(a, b)
    typer.echo(f"{first.name}: {result.size_a} jobs ({', '.join(a['models'])})")
    typer.echo(f"{second.name}: {result.size_b} jobs ({', '.join(b['models'])})")
    typer.echo(f"shared {result.shared}, jaccard {result.jaccard:.2f}")
    if result.rank_correlation is not None:
        typer.echo(f"rank correlation on shared jobs {result.rank_correlation:.2f}")
    if result.mean_score_delta is not None:
        typer.echo(f"mean score difference on shared jobs {result.mean_score_delta:.1f}")
    titles = {job["url"]: f"{job['company']} — {job['title']}" for job in a["jobs"] + b["jobs"]}
    for name, only in ((first.name, result.only_a), (second.name, result.only_b)):
        for url in only[:10]:
            typer.echo(f"  only in {name}: {titles[url]}")


# Step 5 -> render the ranked digest from already-scored jobs; deterministic, no model call
@app.command()
def digest(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    out: Annotated[str | None, typer.Option("--out")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 20,
) -> None:
    """Render the ranked digest. Deterministic, no model call."""
    config = _load(config_path)
    store = SqliteStore(config.db_path)
    try:
        # A digest is a read of what is already scored. It makes no model call and takes
        # no run slot, so it never blocks — or is blocked by — a run in the browser.
        frozen = freeze_config(config, store, current_preferences(store, config))
        build_digest_run(config, store, None, frozen, out=out, limit=limit).execute()
    finally:
        store.close()
    typer.echo(f"digest written to {out or config.scoring.digest_path}")


# Step 6 -> run the full pipeline in order: discover -> score -> digest
@app.command()
def run(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    initial: Annotated[bool, typer.Option("--initial")] = False,
) -> None:
    """discover → score → digest."""
    load_dotenv()
    _require_backends(_load(config_path))
    config = _load(config_path)
    with SqliteStore(config.db_path) as store:
        run_id = _begin(store, config, "full", initial=initial)
        PipelineRun(store, config, run_id).execute()
        row = store.get_run(run_id)
        assert row is not None
        typer.echo(f"run {run_id}: {row['state']}, €{row['cost_eur']:.4f}")
        if row["state"] != "succeeded":
            typer.echo(row["failure_message"])
            raise typer.Exit(code=1)


# Step 7 -> fail fast, before any job is loaded, if a required model backend is unreachable
def _require_backends(config: Config) -> None:
    """Fail before the first job is loaded when a model role cannot be served.

    The failure a local runtime produces is "the server is not up" or "the model was
    never pulled", and both are worth hearing in one line at second zero rather than as
    a connection error after an hour of triage.
    """
    report = diagnose(config)
    if report.ok:
        return
    typer.secho(f"model backend unavailable: {report.problem()}", fg=typer.colors.RED)
    typer.echo("run `career-copilot doctor` for the full report")
    raise typer.Exit(code=1)


# Step 8 -> report which model backend serves each role, and whether it is reachable
@app.command()
def doctor(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
) -> None:
    """Report which model backend serves each role, and whether it is reachable."""
    load_dotenv()
    config = _load(config_path)
    report = diagnose(config, check_pii=True)
    typer.echo(profile_line(report.profile))
    hinted: set[tuple[str, str | None]] = set()
    for status in report.statuses:
        mark = "ok  " if status.usable else "FAIL"
        colour = typer.colors.GREEN if status.usable else typer.colors.RED
        where = status.base_url or "remote"
        tools = {True: " tools", False: " no-tools", None: ""}[status.tools]
        typer.secho(
            f"{mark} {status.role:<6} {status.kind}:{status.model} @ {where}{tools}"
            + (f" — {status.detail}" if status.detail else ""),
            fg=colour,
        )
        hint = start_hint(status.kind)
        if not status.reachable and hint and (status.kind, status.base_url) not in hinted:
            # The app never starts a model server; say how, once per server.
            hinted.add((status.kind, status.base_url))
            typer.echo(f"     start it: {hint}  (see deploy/README.md)")
    pii = report.pii
    typer.secho(
        f"{'ok  ' if pii.usable else 'FAIL'} pii    "
        + ("redaction" if pii.enabled else "redaction disabled")
        + (f" — {pii.detail}" if pii.detail else "")
        + ("" if pii.usable else " (run: uv run python -m spacy download en_core_web_lg)"),
        fg=typer.colors.GREEN if pii.usable else typer.colors.RED,
    )
    cv_ok = _report_cv(config)
    if not (report.ok and cv_ok):
        raise typer.Exit(code=1)


def _report_cv(config: Config) -> bool:
    """One line on the CV the next score run would use. A missing CV fails `doctor`, because
    `score` would fail on it."""
    with SqliteStore(config.db_path) as store:
        cv = store.current_cv()
    if cv is not None:
        typer.secho(
            f"ok   cv     version {cv.version} ({cv.filename}), uploaded "
            f"{cv.uploaded_at.date().isoformat()}",
            fg=typer.colors.GREEN,
        )
        return True
    typer.secho("FAIL cv     none stored. Upload one on the CV page.", fg=typer.colors.RED)
    return False


# Step 9 -> serve the API and the built frontend on one port; the human-facing entry point
@app.command()
def ui(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port")] = 8000,
    open_browser: Annotated[bool, typer.Option("--open/--no-open")] = True,
) -> None:
    """Serve the API and the built frontend on one port, and open a browser at it."""
    import threading
    import webbrowser

    import uvicorn

    from career_copilot.adapters.api.static_ui import BUILD_HINT, mount_ui

    load_dotenv()
    # Set before the app module is imported: it reads its config at import time.
    os.environ["CAREER_COPILOT_CONFIG"] = config_path
    # No account is signed in yet, so the saved model settings (kept per account) are not read.
    config = resolve_models(Config.load(config_path), None, os.environ)
    report = diagnose(config, check_pii=True)
    typer.echo(profile_line(report.profile))
    for status in report.statuses:
        state = "ready" if status.usable else (status.detail or "unavailable")
        typer.echo(f"  {status.role}: {status.kind}:{status.model} — {state}")
    if not report.pii.usable:
        # The upload screen's privacy badge is the first thing a new user meets, and a
        # missing spaCy model makes it say only that the file could not be scanned.
        typer.secho(f"redaction unavailable: {report.pii.detail}", fg=typer.colors.YELLOW)
    if not report.ok:
        # A model backend that is down does not stop the UI: preferences, the review
        # screen and the run history are all readable without one.
        typer.secho("scoring will fail until the backend above is fixed", fg=typer.colors.YELLOW)

    from career_copilot.adapters.api.app import app as api

    url = f"http://{host}:{port}"
    if mount_ui(api):
        typer.echo(f"UI at {url}")
    else:
        typer.secho(f"frontend build not found — {BUILD_HINT}", fg=typer.colors.YELLOW)
        typer.echo(f"API only at {url}")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(api, host=host, port=port, log_level="info")


if __name__ == "__main__":
    app()
