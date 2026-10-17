"""The five things the feedback agent can do, and what each of them refuses.

Like the curator's tools, a refusal is a string starting `refused`, never an exception, so
a small model reads the rule and moves on. Each tool opens its own store: pydantic-ai runs
sync tools on a worker thread, and a sqlite connection must stay on the thread that made it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.filters import matching_terms
from career_copilot.domain.intent import MAX_ENTRY_CHARS
from career_copilot.domain.preferences import PreferencesError
from career_copilot.usecases.apply_preferences import apply_preferences
from career_copilot.usecases.pipeline import current_preferences
from career_copilot.usecases.preferences import save_preferences

MIN_LIMIT = 1
MAX_LIMIT = 50
# The page's lowest bar. The agent reads counts at it; it never changes it.
DEFAULT_THRESHOLD = 30
# One message may exclude a few role words and move this many postings to the bin, no more.
MAX_EXCLUDE_TERMS = 5
MAX_TERM_CHARS = 40
MAX_DISMISS = 100

Refresh = Callable[[Config], dict[str, object]]


class Action(BaseModel):
    """One thing the page should now show or apply. camelCase on the wire."""

    model_config = ConfigDict(populate_by_name=True)

    kind: Literal["set_view", "intent_added", "refresh", "jobs_changed"]
    limit: int | None = None
    include_below: bool | None = Field(default=None, alias="includeBelow")
    text: str | None = None
    started: bool | None = None
    reason: str | None = None
    # The run a started search belongs to, so the page can follow it.
    run_id: int | None = Field(default=None, alias="runId")
    # `jobs_changed`: the excluded words and how many postings went to the Recycle bin.
    terms: list[str] | None = None
    count: int | None = None


@dataclass(slots=True)
class ViewState:
    """The list as the page currently shows it."""

    limit: int
    include_below: bool
    threshold: int = DEFAULT_THRESHOLD


@dataclass(slots=True)
class AssistantDeps:
    """What the tools may touch. Never global state."""

    config: Config
    view: ViewState
    refresh: Refresh
    now: Callable[[], datetime]
    actions: list[Action] = field(default_factory=list)


def view_counts(deps: AssistantDeps) -> str:
    """How many jobs sit above the bar, how many below, and what the page shows now."""
    with SqliteStore(deps.config.db_path) as store:
        page = store.review_page(deps.view.threshold, deps.view.include_below, MIN_LIMIT)
    return (
        f"{page['kept']} jobs are above the bar, {page['below']} are below it and "
        f"{page['unscored']} are not scored yet. The page shows up to {deps.view.limit} "
        f"({'including' if deps.view.include_below else 'without'} lower matches)."
    )


def set_view(deps: AssistantDeps, limit: int, include_below: bool) -> str:
    """Change how many jobs the page lists, and whether lower matches are included."""
    if limit < MIN_LIMIT:
        return f"refused: the limit must be at least {MIN_LIMIT}"
    limit = min(limit, MAX_LIMIT)
    deps.view = ViewState(limit, include_below, deps.view.threshold)
    deps.actions.append(Action(kind="set_view", limit=limit, includeBelow=include_below))
    return f"the page now shows up to {limit} jobs" + (
        ", including lower matches" if include_below else ""
    )


def _refresh(deps: AssistantDeps) -> str:
    result = deps.refresh(deps.config.model_copy(deep=True))
    started = bool(result.get("started"))
    reason = result.get("reason")
    run_id = result.get("runId")
    deps.actions.append(
        Action(
            kind="refresh",
            started=started,
            reason=None if started else str(reason),
            runId=run_id if isinstance(run_id, int) and not isinstance(run_id, bool) else None,
        )
    )
    return "a new search started" if started else f"no search started: {reason}"


def refresh_jobs(deps: AssistantDeps) -> str:
    """Start a new job search."""
    return _refresh(deps)


def add_intent(deps: AssistantDeps, text: str) -> str:
    """Add one line to what the user is looking for, then refresh the recommendations."""
    text = text.strip()
    if not text:
        return "refused: write what the user is looking for"
    if len(text) > MAX_ENTRY_CHARS:
        return f"refused: keep one entry under {MAX_ENTRY_CHARS} characters"
    with SqliteStore(deps.config.db_path) as store:
        store.add_intent(text, deps.now())
    deps.actions.append(Action(kind="intent_added", text=text))
    return f"saved to the preferences; {_refresh(deps)}"


def exclude_roles(deps: AssistantDeps, terms: list[str]) -> str:
    """Never show roles whose title has these words, and move the listed ones to the bin.

    Two effects, both undoable: the words join the saved exclusions (so stored and future
    postings are filtered), and the postings the page lists now go to the Recycle bin.
    Saved and applied postings are never touched.
    """
    cleaned = tuple(dict.fromkeys(t.strip().lower() for t in terms if t.strip()))
    if not cleaned:
        return "refused: name the role words to exclude, for example manager"
    if len(cleaned) > MAX_EXCLUDE_TERMS:
        return f"refused: exclude at most {MAX_EXCLUDE_TERMS} words at a time"
    if any(len(t) > MAX_TERM_CHARS for t in cleaned):
        return f"refused: keep each word under {MAX_TERM_CHARS} characters"
    config = deps.config
    with SqliteStore(config.db_path) as store:
        current = current_preferences(store, config)
        listed = store.listed_titles(deps.view.threshold, deps.view.include_below)
        hits = [job_id for job_id, title in listed if matching_terms(title, cleaned)]
        if len(hits) > MAX_DISMISS:
            return (
                f"refused: {len(hits)} listed postings match, over the limit of {MAX_DISMISS}; "
                "ask the user for a narrower word"
            )
        merged = tuple(dict.fromkeys([*current.excluded_role_terms, *cleaned]))
        if merged != tuple(current.excluded_role_terms):
            try:
                with store.transaction():
                    saved = save_preferences(
                        store, replace(current, excluded_role_terms=merged), deps.now
                    )
                    apply_preferences(
                        store,
                        saved,
                        model=config.scoring.match_model,
                        rubric_version=config.scoring.rubric_version,
                    )
            except PreferencesError as error:
                return f"refused: {'; '.join(error.errors.values())}"
        for job_id in hits:
            store.record_decision(
                job_id, "dismissed", f"excluded role: {', '.join(cleaned)}", deps.now()
            )
    deps.actions.append(Action(kind="jobs_changed", terms=list(cleaned), count=len(hits)))
    return (
        f"excluded {', '.join(cleaned)} from now on; moved {len(hits)} listed "
        "postings to the Recycle bin"
    )
