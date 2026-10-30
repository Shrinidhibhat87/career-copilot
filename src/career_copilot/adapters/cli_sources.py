"""`career-copilot sources ...` — propose boards, review them, list what is crawled.

A driving adapter: composition and printing only. Which proposals exist and what
approval means live in the use case and the store.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Annotated

import typer
from dotenv import load_dotenv

from career_copilot.adapters import cli_user
from career_copilot.adapters.llm.backends import probe
from career_copilot.adapters.observability import agent_recording
from career_copilot.adapters.pii.factory import load_overrides
from career_copilot.adapters.search.tavily import KEY_ENV, TavilySearch
from career_copilot.adapters.sources.company_lists import CsvCompanyList
from career_copilot.adapters.sources.notion import KEY_ENV as NOTION_KEY_ENV
from career_copilot.adapters.sources.notion import NotionCompanyList, NotionError
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.discovery.agent import DiscoveryLimits, build_model
from career_copilot.config import Config
from career_copilot.domain.budget import RunLimits
from career_copilot.domain.company_resolution import ResolvedCompany
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.domain.sources import CompanyRef, ImportStatus, ProposalStatus
from career_copilot.ports import CompanyListProvider
from career_copilot.usecases.discover_sources import SearchUnavailable, build_run
from career_copilot.usecases.find_board import AddBoardError, add_board, find_board
from career_copilot.usecases.import_sources import CompanyListUnavailable
from career_copilot.usecases.import_sources import build_run as build_import_run
from career_copilot.usecases.model_settings import in_force
from career_copilot.usecases.pipeline import build_sources, crawl_targets, current_preferences
from career_copilot.usecases.set_board import SetBoardError, set_board
from career_copilot.usecases.source_health import (
    UnknownSource,
    resume_source,
    sources_to_test,
    try_source,
)

app = typer.Typer(help="Find, review and list the job boards this crawler visits.")


def _now() -> datetime:
    return datetime.now(UTC)


@app.command()
def discover(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    target: Annotated[int | None, typer.Option("--target", min=1)] = None,
    searches: Annotated[int | None, typer.Option("--limit-searches", min=1)] = None,
    budget: Annotated[float | None, typer.Option("--budget", min=0.01)] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Propose boards the crawler does not know about. Nothing is crawled until approved."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(message)s")
    load_dotenv()
    config = in_force(cli_user.load(config_path))
    spec = config.discovery
    if not os.environ.get(KEY_ENV):
        # Checked before anything else is built: without search the agent can only
        # re-derive companies the store already knows, and a model would be loaded for
        # a run that cannot succeed.
        typer.secho(
            f"source discovery needs a search provider — set {KEY_ENV} in .env",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    search = TavilySearch()
    role = config.role("discovery")
    backend_status = probe(role)
    if not backend_status.usable or backend_status.tools is False:
        detail = backend_status.detail or "tool calling is unavailable"
        typer.secho(
            f"discovery model unavailable: {role.kind}:{role.model} — {detail}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)

    with SqliteStore(config.db_path) as store:
        prefs = current_preferences(store, config)
        started = _now()
        run_id = None if dry_run else store.start_run(started, kind="source_discovery")
        try:
            run = build_run(
                build_model(role),
                store,
                search,
                prefs,
                owner_identity(config),
                started,
                run_id=run_id,
                target_proposals=target or spec.target_proposals,
                max_searches=searches or spec.max_searches,
                max_eur=spec.max_eur_per_run if budget is None else budget,
                limits=DiscoveryLimits(spec.max_requests, spec.max_tool_calls),
                run_limits=RunLimits(max_seconds=spec.max_seconds),
                persist=not dry_run,
                profile=store.latest_search_profile(),
                recording=agent_recording(config, role, "discovery", run_id),
            )
        except SearchUnavailable as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
        report = run.execute()
        if run_id is not None:
            store.record_run_cost(run_id, calls=report.searches, cost_eur=report.cost_eur)
            store.finish_run(run_id, _now(), report.summary or report.stopped_because)

    verb = "found" if dry_run else "proposed"
    typer.echo(
        f"{verb} {report.found} board(s): {report.stored} stored, "
        f"{report.duplicates} already known, {report.searches} search(es), "
        f"€{report.cost_eur:.4f}"
    )
    if report.stopped_because:
        typer.secho(f"stopped early: {report.stopped_because}", fg=typer.colors.YELLOW)
    if report.summary:
        typer.echo(report.summary)
    if report.stored:
        typer.echo("review them with: career-copilot sources review")


@app.command("find")
def find(
    name: Annotated[str, typer.Argument(help="Company name, as you would type it.")],
    add: Annotated[bool, typer.Option("--add", help="Add the board if one is found.")] = False,
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
) -> None:
    """Find which job board a company is on from its name. Adds nothing without --add."""
    config = cli_user.load(config_path)
    try:
        lookup = find_board(name)
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from None
    if lookup.hit is None:
        if lookup.blocked_reason:
            typer.secho(
                f"could not check the boards right now: {lookup.blocked_reason}",
                fg=typer.colors.YELLOW,
            )
        else:
            typer.echo(f"no public board found (tried: {', '.join(lookup.tried)})")
        raise typer.Exit(code=1)
    hit = lookup.hit
    typer.echo(f"{lookup.name}: {hit.board_type}/{hit.slug}, {hit.open_roles} open role(s)")
    if not add:
        typer.echo("add it with: career-copilot sources find --add " + repr(lookup.name))
        return
    with SqliteStore(config.db_path) as store:
        try:
            add_board(store, hit.board_type, hit.slug, _now())
        except AddBoardError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
    typer.echo("added to the crawl")


@app.command("review")
def review(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    approve: Annotated[int | None, typer.Option("--approve")] = None,
    reject: Annotated[int | None, typer.Option("--reject")] = None,
) -> None:
    """List pending proposals, or decide one. Approval is the only way into a crawl."""
    config = in_force(cli_user.load(config_path))
    with SqliteStore(config.db_path) as store:
        if approve is not None:
            target = store.approve_proposal(approve, _now())
            if target is None:
                typer.secho(f"proposal {approve} is not pending", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            typer.echo(f"approved: {target.source}/{target.identifier} will be crawled next run")
            return
        if reject is not None:
            if not store.reject_proposal(reject, _now()):
                typer.secho(f"proposal {reject} is not pending", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            typer.echo(f"rejected {reject}; it will not be proposed again")
            return
        pending = store.list_proposals(ProposalStatus.PENDING)
        if not pending:
            typer.echo("no pending proposals")
            return
        for proposal in pending:
            typer.secho(
                f"[{proposal.id}] {proposal.company} — {proposal.board_type}/{proposal.slug} "
                f"({proposal.job_count} jobs, confidence {proposal.confidence:.1f})",
                bold=True,
            )
            typer.echo(f"      evidence: {proposal.evidence}")
            for title in proposal.sample_titles[:3]:
                typer.echo(f"      · {title}")
        typer.echo("approve with --approve ID, reject with --reject ID")


@app.command("list")
def list_targets(
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
) -> None:
    """Every board a run visits: the config.yaml seed, what was approved, what was imported."""
    config = in_force(cli_user.load(config_path))
    with SqliteStore(config.db_path) as store:
        origins = store.imported_target_origins()
        for target in crawl_targets(config, store, current_preferences(store, config)):
            # A board absent from source_targets came from the config.yaml seed.
            origin = origins.get((target.source, target.identifier), "config")
            typer.echo(f"{target.source}/{target.identifier}  ({origin}, tier {target.tier})")


def _provider(config: Config, list_source: str) -> CompanyListProvider | None:
    """`None` when the chosen source is not configured, so the use case refuses with
    one message instead of each adapter inventing its own."""
    if list_source == "notion":
        database_id = config.company_lists.notion.database_id
        return NotionCompanyList(database_id) if database_id else None
    path = config.company_lists.csv.path
    return CsvCompanyList(path) if path else None


def _progress(ref: CompanyRef, outcome: ResolvedCompany | None) -> None:
    if outcome is None:
        typer.echo(f"  = {ref.name}: already resolved, not re-probed")
    elif outcome.status is ImportStatus.RESOLVED:
        typer.secho(f"  + {ref.name}: {outcome.board_type}/{outcome.slug}", fg=typer.colors.GREEN)
    elif outcome.status is ImportStatus.BLOCKED:
        typer.secho(f"  ! {ref.name}: {outcome.last_error}", fg=typer.colors.YELLOW)
    else:
        typer.echo(f"  - {ref.name}: {outcome.last_error or 'no public board found'}")


@app.command("sync")
def sync(
    from_: Annotated[str | None, typer.Option("--from", help="notion | csv")] = None,
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
    refresh: Annotated[bool, typer.Option("--refresh")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Sync a curated company list into the crawl. No LLM, no search, no approval step."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # One probe per board type per slug variant means httpx at INFO drowns out the
    # per-company progress lines, which are the thing worth reading here.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    config = in_force(cli_user.load(config_path))
    list_source = from_ or config.company_lists.configured()
    if list_source is None:
        typer.secho(
            "no company list is configured — set company_lists.notion.database_id or "
            "company_lists.csv.path in config.yaml",
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=1)
    if list_source not in ("notion", "csv"):
        typer.secho(f"unknown company list source '{list_source}'", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if list_source == "notion" and not os.environ.get(NOTION_KEY_ENV):
        # Checked before anything is built: the database cannot be read without it, and
        # Notion answers an empty token with a 401 that says less than this does.
        typer.secho(
            f"reading a Notion database needs {NOTION_KEY_ENV} in .env", fg=typer.colors.RED
        )
        raise typer.Exit(code=1)

    provider = _provider(config, list_source)
    if dry_run:
        # Zero writes, zero probes: this exists to prove the list itself reads cleanly.
        if provider is None:
            typer.secho(f"{list_source} is not configured", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        refs = _fetch(provider)
        typer.echo(f"{list_source}: {len(refs)} company(ies), nothing written")
        for ref in refs:
            typer.echo(f"  · {ref.name}{f'  <{ref.url}>' if ref.url else ''}")
        return

    with SqliteStore(config.db_path) as store:
        started = _now()
        run_id = store.start_run(started, kind="company_sync")
        try:
            run = build_import_run(
                provider,
                store,
                list_source,
                started,
                refresh=refresh,
                on_company=_progress,
            )
        except CompanyListUnavailable as exc:
            store.fail_run(run_id, _now(), "error", str(exc))
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
        try:
            report = run.execute()
        except NotionError as exc:
            store.fail_run(run_id, _now(), "error", str(exc))
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
        store.finish_run(run_id, _now(), report.summary)

    typer.echo(
        f"fetched {report.fetched} companies: {len(report.resolved)} resolved (active), "
        f"{len(report.unresolved)} unresolved, {len(report.blocked)} blocked, "
        f"{len(report.deactivated)} deactivated (removed from list)"
    )
    if report.blocked:
        typer.secho("blocked:", fg=typer.colors.YELLOW)
        for ref, reason in report.blocked:
            typer.echo(f"  - {ref.name}: {reason}")
    if report.unresolved:
        typer.echo("unresolved:")
        for ref in report.unresolved:
            typer.echo(f"  - {ref.name}")


@app.command("set-board")
def set_board_command(
    company: Annotated[str, typer.Argument(help="The company's name, as in your list")],
    board: Annotated[str, typer.Argument(help="A careers-page link, or vendor/slug")],
    from_: Annotated[str | None, typer.Option("--from", help="notion | csv")] = None,
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
) -> None:
    """Point a company the resolver could not find at its real board, then crawl it."""
    config = in_force(cli_user.load(config_path))
    list_source = from_ or config.company_lists.configured()
    if list_source is None:
        typer.secho("no company list is configured", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    with SqliteStore(config.db_path) as store:
        matches = [
            row
            for row in store.list_import_companies(list_source)
            if row.name.casefold() == company.strip().casefold()
        ]
        if not matches:
            typer.secho(
                f"'{company}' is not in the {list_source} list. Run `sources sync` first, "
                "and use the name exactly as listed.",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)
        try:
            board_type, slug, roles = set_board(store, matches[0], board, _now())
        except SetBoardError as exc:
            typer.secho(str(exc), fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
    typer.secho(
        f"{matches[0].name}: {board_type}/{slug} ({roles} open role(s)) — pinned, and crawled "
        "from the next run",
        fg=typer.colors.GREEN,
    )


def _fetch(provider: CompanyListProvider) -> list[CompanyRef]:
    try:
        return list(provider.fetch())
    except NotionError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from None
    except OSError as exc:
        typer.secho(f"could not read the company list: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from None


def owner_identity(config: Config) -> OwnerIdentity:
    """What the search guard refuses to put in a query.

    The pinned-identity file if there is one, and otherwise nothing: the agent's prompts
    carry company names and cities, so an empty identity blocks nothing that should have
    been blocked. A user who wants the guard to know a name pins it, as the redactor
    already documents.
    """
    return load_overrides(config.pii.identity_path) or OwnerIdentity()


@app.command("test")
def test_sources(
    source: Annotated[
        str | None, typer.Argument(help="One source; all with a board if omitted")
    ] = None,
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
) -> None:
    """Send one real request per source and say whether it answers. Writes nothing."""
    config = in_force(cli_user.load(config_path))
    adapters = build_sources()
    with SqliteStore(config.db_path) as store:
        names = [source] if source else list(sources_to_test(store, config, adapters))
        failed = False
        for name in names:
            try:
                result = try_source(store, config, adapters, name)
            except UnknownSource:
                typer.secho(f"no job source named {name}", fg=typer.colors.RED)
                raise typer.Exit(code=1) from None
            if result.ok:
                typer.secho(
                    f"ok   {name} — {result.fetched} found in {result.duration_ms} ms",
                    fg=typer.colors.GREEN,
                )
            else:
                failed = True
                typer.secho(f"FAIL {name} — {result.message}", fg=typer.colors.RED)
    if failed:
        raise typer.Exit(code=1)


@app.command("resume")
def resume_command(
    source: Annotated[str, typer.Argument(help="A paused source")],
    config_path: Annotated[str, typer.Option("--config")] = "config.yaml",
) -> None:
    """Cut a pause short, so the next run retries the source. Fetches nothing itself."""
    config = in_force(cli_user.load(config_path))
    with SqliteStore(config.db_path) as store:
        try:
            state = resume_source(store, build_sources(), source, _now())
        except UnknownSource:
            typer.secho(f"no job source named {source}", fg=typer.colors.RED)
            raise typer.Exit(code=1) from None
    typer.echo(f"{source}: {state}; the next run will retry it")
