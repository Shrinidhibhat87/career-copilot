"""FastAPI driving adapter over the resume and preferences use cases.

A driving adapter only: it composes existing layers and adds no coupling to them. Every
rule it appears to apply lives in the domain or a use case; what belongs here is the
mapping between HTTP and those calls, including which failure becomes which status.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast

from dotenv import load_dotenv
from fastapi import Body, FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from career_copilot.adapters import accounts
from career_copilot.adapters.api import (
    account,
    applications,
    assistant,
    auth,
    company_lists,
    health,
    intent,
    model_settings,
    observability,
    outline,
    recycle_bin,
    review,
    runs,
    sample_roles,
    source_lookup,
)
from career_copilot.adapters.llm.backends import BackendUnavailable, build_client
from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.observability import recorder_for
from career_copilot.adapters.pii.factory import build_redactor
from career_copilot.adapters.profile_loader import load_notes
from career_copilot.adapters.resume.extract import ResumeExtractionError, extract
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.cv import StoredCv
from career_copilot.domain.preferences import (
    CEFR_LEVELS,
    MAX_ROLE_FAMILIES,
    SENIORITY_BANDS,
    CefrLevel,
    LanguageSkill,
    Preferences,
    PreferencesError,
    SeniorityBand,
    bootstrap,
    normalize_term,
    validate,
)
from career_copilot.domain.resume_detect import detect, label_for
from career_copilot.ports import LLMCallError, LLMClient
from career_copilot.usecases.apply_preferences import apply_preferences, plan_refilter
from career_copilot.usecases.extract_preferences import propose_from_notes
from career_copilot.usecases.model_settings import in_force
from career_copilot.usecases.parse_resume import ParsedResume, TextRedactor, analyse_resume
from career_copilot.usecases.preferences import load_preferences, save_preferences

MAX_BYTES = 5 * 1024 * 1024
_CHUNK = 64 * 1024
_ALLOWED_SUFFIXES = {".pdf", ".docx"}
_ALLOWED_TYPES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_log = logging.getLogger(__name__)

_config: Config


def warm_analyzer(redactor: object | None) -> None:
    """Build the PII analyzer ahead of the first request. Never raises: a failed warm-up
    only means the first read pays the build cost, as it did before."""
    try:
        warm = getattr(getattr(redactor, "detector", None), "warm", None)
        if callable(warm):
            warm()
    except (Exception, SystemExit) as exc:
        _log.warning("PII analyzer warm-up failed (%s)", type(exc).__name__)


_pipeline_lifespan = runs.make_lifespan()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Its own daemon thread, not the pipeline's one-worker executor: a run started right
    # after boot must not queue behind a model load.
    threading.Thread(
        target=warm_analyzer, args=(_redactor,), name="pii-warmup", daemon=True
    ).start()
    async with _pipeline_lifespan(app):
        yield


def _user_config() -> Config:
    """The loaded config pointed at the signed-in person's database (spec 015). Every
    route behind the gate reads this; the gate guarantees someone is signed in."""
    user = auth.current_user()
    if user is None:
        raise HTTPException(401, "Sign in first.")
    return accounts.for_user(_config, user)


def _on_login(name: str) -> None:
    config = _user_config()
    recycle_bin.clean_expired(config)
    runs.start_session(config)


def _in_force() -> Config:
    """The config with the saved model settings applied, read per request (spec 014).
    Routers that call a model get this; the rest keep the loaded config."""
    return in_force(_user_config())


app = FastAPI(title="Career Copilot", lifespan=_lifespan)
app.include_router(runs.router(_in_force))
app.include_router(health.router(_in_force))
app.include_router(model_settings.router(lambda: _user_config()))
app.include_router(observability.router(lambda: _user_config()))
app.include_router(review.router(lambda: _user_config()))
app.include_router(outline.router(_in_force))
app.include_router(assistant.router(_in_force, on_refresh=runs.request_refresh))
app.include_router(recycle_bin.router(lambda: _user_config()))
app.include_router(applications.router(lambda: _user_config()))
app.include_router(company_lists.router(lambda: _user_config()))
app.include_router(source_lookup.router(_in_force, web_search=source_lookup.agent_search))
app.include_router(intent.router(_in_force, on_change=runs.request_refresh))
app.include_router(sample_roles.router(_in_force, on_change=runs.request_refresh))
app.include_router(account.router(lambda: _user_config()))
app.include_router(
    auth.router(
        lambda: _config,
        on_login=_on_login,
        on_logout=runs.end_session,
        run_active=runs.run_active,
    )
)
auth.install(app)
app.add_middleware(
    CORSMiddleware,
    # One development origin. Widening this is a deployment decision, not a default.
    allow_origins=[os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)

# The same read `cli.py` does at the top of a command. The server is its own process and
# inherits nothing from a shell that once ran the CLI, so without this an OPENROUTER_API_KEY
# sitting in .env is invisible here and every extraction answers 409 forever.
load_dotenv()

# `career-copilot ui --config` sets this before importing the app; a bare uvicorn start
# reads config.yaml as before.
_config = Config.load(os.environ.get("CAREER_COPILOT_CONFIG", "config.yaml"))


_redactor: TextRedactor | None = build_redactor(_config)

# Detection and the redacted preview are the slow part of a read. They are a pure function
# of the CV text and the skill vocabulary, so the answer is kept in memory (never in SQLite)
# under exactly those inputs.
_resume_cache: dict[tuple[str, tuple[str, ...]], ParsedResume] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _store() -> SqliteStore:
    """One short-lived connection per request. SQLite connections are not shared across
    threads, and a request handler is the natural unit of work here."""
    return SqliteStore(_user_config().db_path)


def _payload(parsed: ParsedResume) -> dict[str, object]:
    detected = parsed.detections
    return {
        "filename": parsed.filename,
        "parsedAt": parsed.parsed_at.isoformat(),
        "summary": {
            "roles": len(detected.roles),
            "skills": len(detected.skills),
            "languages": len(detected.languages),
        },
        "privacy": {
            "enabled": parsed.privacy.enabled,
            "counts": dict(parsed.privacy.counts),
            # The full redacted text, not a snippet. `null` means none exists: redaction is
            # off, or the detector failed on this file. It is never the original text.
            "redactedPreview": parsed.privacy.redacted_preview,
            # Confidence level only — `confirmed`, `likely`, or `none`. Never a value.
            "ownerAnchor": parsed.privacy.owner_anchor,
            # Why there is no preview, when there is none. Safe to show: a failure class or
            # a named missing model, never text or a detected value.
            "unavailableReason": parsed.privacy.unavailable_reason,
        },
        "detected": {
            "roles": list(detected.roles),
            "skills": list(detected.skills),
            "languages": [
                {"name": language.name, "level": language.level} for language in detected.languages
            ],
        },
    }


async def _read_capped(upload: UploadFile) -> bytes:
    """Read the upload, refusing it the moment it passes the limit."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await upload.read(_CHUNK):
        total += len(chunk)
        if total > MAX_BYTES:
            raise HTTPException(413, "That file is over the 5 MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _skill_terms() -> Sequence[str]:
    # The skill vocabulary is the preferences' own, not `config.yaml`: detection and the
    # hard filters read one list, so a chip on the resume screen is a term that actually
    # matches postings.
    store = _store()
    try:
        return load_preferences(store, _config_seed()).core_skills
    finally:
        store.close()


def _cv_payload(cv: StoredCv, parsed: ParsedResume, *, changed: bool) -> dict[str, object]:
    return {
        **_payload(parsed),
        "version": cv.version,
        "sha256": cv.sha256,
        "changed": changed,
    }


@app.post("/api/resume")
async def upload_resume(file: Annotated[UploadFile, File()]) -> dict[str, object]:
    filename = file.filename or ""
    if Path(filename).suffix.lower() not in _ALLOWED_SUFFIXES or (
        file.content_type is not None and file.content_type not in _ALLOWED_TYPES
    ):
        raise HTTPException(415, "Only PDF and DOCX files are supported.")
    data = await _read_capped(file)
    skill_terms = _skill_terms()
    now = datetime.now(UTC)
    store = _store()
    try:
        text = extract(filename, data)
        saved = store.save_cv(filename, text, now)
        parsed = analyse_resume(filename, text, now, skill_terms, _redactor)
        _resume_cache.clear()
    except ResumeExtractionError as error:
        raise HTTPException(422, "Could not read any text from that file.") from error
    except Exception as error:
        raise HTTPException(502, "Something went wrong reading that file.") from error
    finally:
        store.close()
    return _cv_payload(saved.cv, parsed, changed=saved.changed)


def _current_cv() -> StoredCv | None:
    store = _store()
    try:
        return store.current_cv()
    finally:
        store.close()


@app.get("/api/resume")
async def read_resume() -> dict[str, object]:
    cv = _current_cv()
    if cv is None:
        raise HTTPException(404, "No resume has been stored.")
    # Derived from the stored text, never stored beside it: the in-memory cache is keyed by
    # the CV hash and skill terms, so a new CV or a changed vocabulary is a miss.
    skill_terms = tuple(_skill_terms())
    key = (cv.sha256, skill_terms)
    parsed = _resume_cache.get(key)
    if parsed is None:
        parsed = analyse_resume(cv.filename, cv.text, cv.uploaded_at, skill_terms, _redactor)
        _resume_cache.clear()
        _resume_cache[key] = parsed
    return _cv_payload(cv, parsed, changed=False)


@app.get("/api/resume/versions")
async def resume_versions() -> list[dict[str, object]]:
    store = _store()
    try:
        versions = store.list_cvs()
    finally:
        store.close()
    return [
        {
            "version": cv.version,
            "filename": cv.filename,
            "uploadedAt": cv.uploaded_at.isoformat(),
            "current": cv.current,
        }
        for cv in versions
    ]


@app.delete("/api/resume", status_code=204)
async def clear_resume() -> Response:
    store = _store()
    try:
        store.delete_cvs()
        _resume_cache.clear()
    finally:
        store.close()
    return Response(status_code=204)


# --- preferences ----------------------------------------------------------------------

# Values the screen renders as chips travel as {term, label} pairs: the server keeps the
# match term, and the browser never has to guess how one is displayed.
_ORIGIN_RESUME = "resume"


def _as_level(value: object) -> CefrLevel:
    """A submitted CEFR level, carried through as given.

    Not corrected or dropped here: `domain.preferences.validate` is what rejects an
    unknown level, and it can name the offending language while this function cannot.
    """
    return cast(CefrLevel, str(value).strip())


def _as_band(value: object) -> SeniorityBand:
    """A submitted seniority band, carried through as given. Validated in the domain."""
    return cast(SeniorityBand, str(value).strip().lower())


def _config_seed() -> Preferences:
    return bootstrap(
        _config.filters.to_filter_config(),
        notes=load_notes(_config.profile_dir),
        batch_size=_config.scoring.incremental_target,
    )


def _terms(
    values: tuple[str, ...], prefilled: frozenset[str] = frozenset()
) -> list[dict[str, Any]]:
    return [
        {"term": term, "label": label_for(term), "fromResume": term in prefilled} for term in values
    ]


def _merge_detections(prefs: Preferences) -> tuple[Preferences, frozenset[str]]:
    """While no version has been saved, the uploaded resume's detections prefill the form.

    Merged rather than substituted: the config seed is still the honest starting point,
    and the detections add to it. Once a version exists the user has spoken, and nothing
    here may override that.
    """
    cv = _current_cv() if prefs.version == 0 else None
    if cv is None:
        return prefs, frozenset()
    detected = detect(cv.text, _skill_terms())
    prefilled: set[str] = set()

    def merge(existing: tuple[str, ...], found: tuple[str, ...]) -> tuple[str, ...]:
        added = tuple(term for term in found if term not in existing)
        prefilled.update(found)
        return (*existing, *added)

    languages = list(prefs.languages)
    for language in detected.languages:
        # A language with no stated level cannot fill a CEFR field, and guessing one would
        # be inventing a claim the resume never made.
        if language.level in CEFR_LEVELS:
            languages = [item for item in languages if item.key != language.name.lower()]
            languages.append(LanguageSkill(name=language.name, level=_as_level(language.level)))
            prefilled.add(f"language:{language.name.lower()}")
    merged = replace(
        prefs,
        role_families=merge(prefs.role_families, detected.role_terms)[:MAX_ROLE_FAMILIES],
        core_skills=merge(prefs.core_skills, detected.skill_terms),
        languages=tuple(languages),
    )
    return merged, frozenset(prefilled)


def _preferences_payload(prefs: Preferences, prefilled: frozenset[str]) -> dict[str, Any]:
    return {
        "version": prefs.version,
        "updatedAt": prefs.updated_at.isoformat() if prefs.updated_at else None,
        "roleIntent": prefs.role_intent,
        "roleFamilies": _terms(prefs.role_families, prefilled),
        "titleTerms": _terms(prefs.title_terms, prefilled),
        "coreSkills": _terms(prefs.core_skills, prefilled),
        "excludedStack": _terms(prefs.excluded_stack, prefilled),
        "excludedRoleTerms": _terms(prefs.excluded_role_terms, prefilled),
        "seniority": list(prefs.seniority),
        "allowedCities": _terms(prefs.allowed_cities, prefilled),
        "preferredCities": _terms(prefs.preferred_cities, prefilled),
        "flaggedCities": _terms(prefs.flagged_cities, prefilled),
        "allowRemote": prefs.allow_remote,
        "requirePermanent": prefs.require_permanent,
        "languages": [
            {
                "name": language.name,
                "level": language.level,
                "fromResume": f"language:{language.name.lower()}" in prefilled,
            }
            for language in prefs.languages
        ],
        "maxPerCompany": prefs.max_per_company,
        "batchSize": prefs.batch_size,
        "notes": prefs.notes,
        "seniorityBands": list(SENIORITY_BANDS),
        "cefrLevels": list(CEFR_LEVELS),
    }


def _term_list(body: dict[str, Any], key: str) -> tuple[str, ...]:
    """Accept either a bare term or a {term, label} pair, and keep only the term."""
    values = body.get(key, [])
    if not isinstance(values, list):
        return ()
    terms: list[str] = []
    for value in values:
        raw = value.get("term", "") if isinstance(value, dict) else value
        term = normalize_term(str(raw))
        if term and term not in terms:
            terms.append(term)
    return tuple(terms)


def _preferences_from_body(body: dict[str, Any], current: Preferences) -> Preferences:
    """Read a submitted form. Values are normalized here; validity is the domain's call."""
    errors: dict[str, str] = {}
    for key, field in (("allowRemote", "allow_remote"), ("requirePermanent", "require_permanent")):
        if key in body and type(body[key]) is not bool:
            errors[field] = "expected true or false"
    for key, field in (("batchSize", "batch_size"), ("maxPerCompany", "max_per_company")):
        if key in body and type(body[key]) is not int:
            errors[field] = "expected a whole number"
    if errors:
        raise PreferencesError(errors)
    languages = body.get("languages", [])
    return replace(
        current,
        # Free text, so only trimmed — no term normalization. The length cap is the
        # domain's call, which is why nothing is truncated here.
        role_intent=str(body.get("roleIntent", current.role_intent)).strip(),
        role_families=_term_list(body, "roleFamilies"),
        # Sent by the form on every save, so an emptied list is honoured as an emptied
        # list. `Preferences.from_payload` is what treats an *absent* key as unstated.
        title_terms=_term_list(body, "titleTerms"),
        core_skills=_term_list(body, "coreSkills"),
        excluded_stack=_term_list(body, "excludedStack"),
        excluded_role_terms=_term_list(body, "excludedRoleTerms"),
        seniority=tuple(_as_band(band) for band in body.get("seniority", []) if str(band).strip()),
        allowed_cities=_term_list(body, "allowedCities"),
        preferred_cities=_term_list(body, "preferredCities"),
        flagged_cities=_term_list(body, "flaggedCities"),
        allow_remote=bool(body.get("allowRemote", current.allow_remote)),
        require_permanent=bool(body.get("requirePermanent", current.require_permanent)),
        languages=tuple(
            LanguageSkill(
                name=str(item.get("name", "")).strip(), level=_as_level(item.get("level"))
            )
            for item in languages
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ),
        max_per_company=int(body.get("maxPerCompany", current.max_per_company)),
        batch_size=int(body.get("batchSize", current.batch_size)),
        notes=str(body.get("notes", "")),
    )


@app.get("/api/preferences")
async def read_preferences() -> dict[str, object]:
    store = _store()
    try:
        prefs = load_preferences(store, _config_seed())
    finally:
        store.close()
    merged, prefilled = _merge_detections(prefs)
    return _preferences_payload(merged, prefilled)


@app.put("/api/preferences")
async def write_preferences(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
    store = _store()
    try:
        with store.transaction():
            current = load_preferences(store, _config_seed())
            try:
                saved = save_preferences(store, _preferences_from_body(body, current), _now)
            except PreferencesError as error:
                # 422 with one message per field: the screen marks the offending controls
                # rather than showing a single sentence about the form as a whole.
                raise HTTPException(422, {"errors": error.errors}) from error
            except (TypeError, ValueError) as error:
                raise HTTPException(
                    422, {"errors": {"form": "Could not read that value."}}
                ) from error
            # Free and immediate: stored jobs carry the outcome computed at discover time, so
            # without this the save would appear to change nothing at all.
            report = apply_preferences(
                store,
                saved,
                model=_config.scoring.match_model,
                rubric_version=_config.scoring.rubric_version,
            )
    finally:
        store.close()
    return {
        "preferences": _preferences_payload(saved, frozenset()),
        "refiltered": {
            "examined": report.examined,
            "changed": report.changed,
            "transitions": report.transitions,
            "nowPassing": report.now_passing,
        },
        "reopenedForScoring": report.reopened_for_scoring,
    }


@app.post("/api/preferences/preview")
async def preview_preferences(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
    """What saving this draft would do to the jobs already stored. Writes nothing."""
    store = _store()
    try:
        try:
            draft = _preferences_from_body(body, load_preferences(store, _config_seed()))
            validate(draft)
        except PreferencesError as error:
            raise HTTPException(422, {"errors": error.errors}) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(422, {"errors": {"form": "Could not read that value."}}) from error
        plan = plan_refilter(store.all_active_jobs(), draft)
    finally:
        store.close()
    return {
        "examined": plan.examined,
        "changed": len(plan.changes),
        "transitions": plan.transitions,
        "nowPassing": plan.now_passing,
        "hidden": plan.hidden,
        "excludedRoleTerms": {
            term: {"matches": impact.matches, "newlyHidden": impact.newly_hidden}
            for term, impact in plan.excluded_role_terms.items()
        },
    }


def _extraction_client() -> LLMClient:
    """The same composition `cli.py` uses: the provider client wrapped in the redaction
    boundary, so notes never reach a provider unredacted."""
    config = _in_force()
    client: LLMClient = build_client(config.role("triage"), recorder_for(config))
    redactor = build_redactor(_config)
    if redactor is not None:
        client = RedactingLLMClient(client, redactor, outbound=_config.pii.outbound)
    return client


@app.post("/api/preferences/extract")
async def extract_preferences_route(body: Annotated[dict[str, Any], Body()]) -> dict[str, object]:
    try:
        client = _extraction_client()
    except BackendUnavailable as exc:
        raise HTTPException(409, str(exc)) from None
    store = _store()
    try:
        current = load_preferences(store, _config_seed())
    finally:
        store.close()
    notes = str(body.get("notes", ""))
    if "preferences" in body:
        try:
            draft = body["preferences"]
            if not isinstance(draft, dict):
                raise PreferencesError({"form": "Expected preferences object."})
            current = _preferences_from_body(draft, current)
            validate(current)
        except PreferencesError as error:
            raise HTTPException(422, {"errors": error.errors}) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(422, {"errors": {"form": "Could not read that value."}}) from error
    try:
        result = propose_from_notes(notes, replace(current, notes=notes), client)
    except LLMCallError as error:
        raise HTTPException(502, "The model could not be reached.") from error
    return {
        "changes": [
            {
                "field": change.field,
                "action": change.action,
                "value": change.value,
                "label": label_for(change.value),
                "rationale": change.rationale,
            }
            for change in result.changes
        ],
        "costEur": result.cost_eur,
    }
