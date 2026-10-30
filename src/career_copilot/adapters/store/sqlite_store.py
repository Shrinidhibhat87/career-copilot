"""SQLite StateStore. Canonical state for local runs."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from array import array
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from career_copilot.adapters.store.migrations import migrate
from career_copilot.domain.company_resolution import extract_ats_hint
from career_copilot.domain.cv import SavedCv, StoredCv, cv_sha256
from career_copilot.domain.feedback import FeedbackEntry
from career_copilot.domain.intent import IntentEntry, IntentKind
from career_copilot.domain.lifecycle import LABEL_TO_DECISION, BinItem
from career_copilot.domain.model_settings import ModelSettings, parse_overrides, parse_preset
from career_copilot.domain.models import (
    DigestStats,
    FilterOutcome,
    HardFilterResult,
    JobPosting,
    JobStatus,
    ScoreStage,
    SourceTarget,
    StoredJob,
    UpsertOutcome,
)
from career_copilot.domain.preference_note import DecisionSignal
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import Progress, RunConfig, RunInProgress, stalled
from career_copilot.domain.sample_roles import SampleRole
from career_copilot.domain.scoring import Score, ScoredJob, ScoreVerdict
from career_copilot.domain.search_profile import SearchProfile
from career_copilot.domain.source_health import FailureReason, SourceHealth
from career_copilot.domain.sources import (
    BoardVisit,
    CompanyRef,
    ImportCompany,
    ImportStatus,
    ProposalStatus,
    SourceProposal,
)
from career_copilot.usecases.embed_jobs import job_text, text_hash
from career_copilot.usecases.import_sources import DEFAULT_COOLDOWN_HOURS, DEFAULT_TIER

_SEP = ""  # unit separator: reasons never contain it

_MAX_BACKOFF_HOURS = 24 * 7

# Columns refreshed on every re-observation. `source`/`source_job_id` are excluded:
# they are the row's origin, and a match found on URL or fingerprint may come from
# a different board. A difference in any of these means the job needs rescoring.
_MUTABLE_COLUMNS: tuple[str, ...] = (
    "company",
    "company_norm",
    "title",
    "title_norm",
    "location",
    "location_norm",
    "canonical_url",
    "description",
    "fingerprint",
    "content_hash",
    "posted_at",
    "employment_type",
    "remote",
)

# Columns carrying a UNIQUE index. Another row may already own the new value.
_UNIQUE_COLUMNS: tuple[str, ...] = ("canonical_url", "fingerprint")


def _join(values: tuple[str, ...]) -> str:
    return _SEP.join(values)


def _split(value: str) -> tuple[str, ...]:
    return tuple(v for v in value.split(_SEP) if v)


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _stage_seconds(started_at: str, ended_at: str | None) -> float | None:
    """How long a finished stage took; None for a stage that never ended."""
    if not ended_at:
        return None
    return round(
        (datetime.fromisoformat(ended_at) - datetime.fromisoformat(started_at)).total_seconds(), 1
    )


BUSY_TIMEOUT_S = 5.0
# Hashes per `IN (...)` lookup. SQLite's default bind limit is 999 on older builds.
_VECTOR_CHUNK = 500


class SqliteStore:
    """StateStore backed by a local SQLite file (or ':memory:' in tests)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._in_transaction = False
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # Explicit rather than Python's implicit default: a UI write that lands while a run
        # holds the writer waits this long instead of failing. Write transactions stay short
        # and never span an LLM or network call.
        self._conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        migrate(self._conn)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Publish a preferences version and its filter outcomes together."""
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self._conn.commit()
        except BaseException:
            self._conn.rollback()
            raise
        finally:
            self._in_transaction = False

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SqliteStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- jobs -------------------------------------------------------------

    def _find_existing(self, posting: JobPosting) -> sqlite3.Row | None:
        """Dedupe ladder: source id, then canonical URL, then fingerprint."""
        for sql, args in (
            (
                "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
                (posting.source, posting.source_job_id),
            ),
            ("SELECT * FROM jobs WHERE canonical_url = ?", (posting.canonical_url,)),
            ("SELECT * FROM jobs WHERE fingerprint = ?", (posting.fingerprint,)),
        ):
            row: sqlite3.Row | None = self._conn.execute(sql, args).fetchone()
            if row is not None:
                return row
        return None

    @staticmethod
    def _mutable_values(posting: JobPosting) -> dict[str, Any]:
        """Posting fields as they are stored, so row and posting compare directly."""
        return {
            "company": posting.company,
            "company_norm": posting.company_norm,
            "title": posting.title,
            "title_norm": posting.title_norm,
            "location": posting.location,
            "location_norm": posting.location_norm,
            "canonical_url": posting.canonical_url,
            "description": posting.description,
            "fingerprint": posting.fingerprint,
            "content_hash": posting.content_hash,
            "posted_at": posting.posted_at.isoformat() if posting.posted_at else None,
            "employment_type": posting.employment_type,
            "remote": int(bool(posting.remote)),
        }

    def _owned_by_other(self, column: str, value: Any, job_id: int) -> bool:
        # `column` is a module constant, never caller input.
        row = self._conn.execute(
            f"SELECT 1 FROM jobs WHERE {column} = ? AND id != ?",
            (value, job_id),
        ).fetchone()
        return row is not None

    def upsert_job(
        self, posting: JobPosting, verdict: HardFilterResult, now: datetime
    ) -> tuple[int, UpsertOutcome]:
        """Insert, or update an already-known job. Rescore only on content change."""
        existing = self._find_existing(posting)
        if existing is None:
            cur = self._conn.execute(
                """
                INSERT INTO jobs (
                    source, source_job_id, company, company_norm, title, title_norm,
                    location, location_norm, canonical_url, description, fingerprint,
                    content_hash, posted_at, employment_type, remote, filter_outcome,
                    filter_reasons, filter_warnings, status, needs_rescore,
                    first_seen_at, last_seen_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                """,
                (
                    posting.source,
                    posting.source_job_id,
                    posting.company,
                    posting.company_norm,
                    posting.title,
                    posting.title_norm,
                    posting.location,
                    posting.location_norm,
                    posting.canonical_url,
                    posting.description,
                    posting.fingerprint,
                    posting.content_hash,
                    posting.posted_at.isoformat() if posting.posted_at else None,
                    posting.employment_type,
                    int(bool(posting.remote)),
                    str(verdict.outcome),
                    _join(verdict.reasons),
                    _join(verdict.warnings),
                    str(JobStatus.ACTIVE),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            job_id = int(cur.lastrowid or 0)
            self._conn.execute(
                "INSERT INTO job_versions (job_id, content_hash, observed_at) VALUES (?,?,?)",
                (job_id, posting.content_hash, now.isoformat()),
            )
            self._conn.commit()
            return job_id, UpsertOutcome.INSERTED

        job_id = int(existing["id"])
        if existing["tombstoned_at"] is not None:
            # A cleaned job is gone for good. Refilling its description or marking it for
            # rescoring would bring back a job the user threw away.
            return job_id, UpsertOutcome.UNCHANGED
        if existing["source"] != posting.source:
            # The same job seen on another board. Its wording differs ("Acme" vs
            # "Acme GmbH") without the job having changed, so a cross-source sighting
            # only refreshes liveness. Rewriting the fields here would flap them back
            # and forth on every run and rescore the job each time.
            self._conn.execute(
                "UPDATE jobs SET last_seen_at = ?, status = ? WHERE id = ?",
                (now.isoformat(), str(JobStatus.ACTIVE), job_id),
            )
            self._conn.commit()
            return job_id, UpsertOutcome.UNCHANGED

        values = self._mutable_values(posting)
        # A unique column already held by another row stays as it is: two rows that
        # collide on URL or fingerprint are a merge case, not something an upsert
        # decides. Everything else is refreshed.
        for column in _UNIQUE_COLUMNS:
            if values[column] != existing[column] and self._owned_by_other(
                column, values[column], job_id
            ):
                values[column] = existing[column]
        changed = any(existing[column] != values[column] for column in _MUTABLE_COLUMNS)
        versioned = existing["content_hash"] != values["content_hash"]
        # Column names come from the module constant; every value is bound.
        assignments = ", ".join(f"{column} = ?" for column in _MUTABLE_COLUMNS)
        self._conn.execute(
            f"""
            UPDATE jobs SET {assignments},
                            last_seen_at = ?, status = ?,
                            filter_outcome = ?, filter_reasons = ?, filter_warnings = ?,
                            needs_rescore = CASE WHEN ? THEN 1 ELSE needs_rescore END
            WHERE id = ?
            """,
            (
                *(values[column] for column in _MUTABLE_COLUMNS),
                now.isoformat(),
                str(JobStatus.ACTIVE),
                str(verdict.outcome),
                _join(verdict.reasons),
                _join(verdict.warnings),
                int(changed),
                job_id,
            ),
        )
        if versioned:
            self._conn.execute(
                "INSERT INTO job_versions (job_id, content_hash, observed_at) VALUES (?,?,?)",
                (job_id, posting.content_hash, now.isoformat()),
            )
        self._conn.commit()
        return job_id, UpsertOutcome.CHANGED if changed else UpsertOutcome.UNCHANGED

    def list_jobs(
        self, outcome: FilterOutcome | None = None, limit: int = 50
    ) -> Sequence[StoredJob]:
        # Dismissed is permanent, so it never shows in any list, the digest included.
        sql = (
            "SELECT j.* FROM jobs j LEFT JOIN job_decisions d ON d.id = ("
            "SELECT id FROM job_decisions WHERE job_id = j.id ORDER BY id DESC LIMIT 1) "
            f"WHERE j.status = ? AND {self._NOT_DISMISSED}"
        )
        args: list[Any] = [str(JobStatus.ACTIVE)]
        if outcome is not None:
            sql += " AND j.filter_outcome = ?"
            args.append(str(outcome))
        sql += " ORDER BY j.last_seen_at DESC, j.id DESC LIMIT ?"
        args.append(limit)
        return [self._to_stored(row) for row in self._conn.execute(sql, args)]

    def results_page(
        self, outcome: str | None, sort: str, direction: str, page: int, limit: int
    ) -> dict[str, object]:
        # Choose the latest score for the current content, with stable id tie-breaking.
        columns = {
            "score": "m.score",
            "triage": "t.score",
            "title": "j.title",
            "company": "j.company",
            "age": "j.first_seen_at",
            "cost": "COALESCE(t.cost_eur, 0) + COALESCE(m.cost_eur, 0)",
        }
        order = columns[sort]
        ordering = {"asc": "ASC", "desc": "DESC"}[direction]
        where = f"j.status='active' AND {self._NOT_DISMISSED}"
        args: list[Any] = []
        if outcome is not None:
            where += " AND j.filter_outcome=?"
            args.append(outcome)
        decisions = (
            "LEFT JOIN job_decisions d ON d.id = (SELECT id FROM job_decisions "
            "WHERE job_id = j.id ORDER BY id DESC LIMIT 1) "
            "LEFT JOIN job_views v ON v.job_id = j.id"
        )
        total = self._conn.execute(
            f"SELECT COUNT(*) FROM jobs j {decisions} WHERE {where}", args
        ).fetchone()[0]
        rows = self._conn.execute(
            f"""SELECT j.*, t.score AS triage_score, m.score AS match_score,
                t.cost_eur AS triage_cost, m.cost_eur AS match_cost,
                m.preferences_hash AS score_preferences_hash,
                d.decision AS decision, v.viewed_at AS viewed_at
                FROM jobs j
                {decisions}
                LEFT JOIN scores t ON t.id=(SELECT id FROM scores WHERE job_id=j.id
                    AND stage='triage' AND content_hash=j.content_hash ORDER BY id DESC LIMIT 1)
                LEFT JOIN scores m ON m.id=(SELECT id FROM scores WHERE job_id=j.id
                    AND stage='match' AND content_hash=j.content_hash ORDER BY id DESC LIMIT 1)
                WHERE {where} ORDER BY {order} {ordering} NULLS LAST, j.id DESC LIMIT ? OFFSET ?""",
            [*args, limit, (page - 1) * limit],
        )
        items = [
            {
                "id": row["id"],
                "title": row["title"],
                "company": row["company"],
                "location": row["location"],
                "source": row["source"],
                "url": row["canonical_url"],
                "firstSeenAt": row["first_seen_at"],
                "outcome": row["filter_outcome"],
                "triageScore": row["triage_score"],
                "matchScore": row["match_score"],
                "costEur": (
                    None
                    if row["triage_cost"] is None and row["match_cost"] is None
                    else (row["triage_cost"] or 0) + (row["match_cost"] or 0)
                ),
                "preferencesHash": row["score_preferences_hash"],
                "decision": row["decision"],
                "isNew": row["viewed_at"] is None,
            }
            for row in rows
        ]
        return {"items": items, "total": total, "page": page, "limit": limit}

    # --- review -----------------------------------------------------------

    # The score a posting is ranked and cut by: the match score when one exists, the
    # triage score while it does not. A job with neither is not "scoring zero", it is
    # unscored, and the two must never be shown as the same thing.
    # A role outside the user's country that only passes because it is remote is not a
    # recommendation: whatever the model said, it ranks below `LOCATION_MISMATCH_CAP`, under
    # any threshold the slider allows. The rule's own warning is the marker.
    LOCATION_MISMATCH_CAP = 9
    _LOCATION_MISMATCH = "j.filter_warnings LIKE '%remote role with non-German location%'"
    _EFFECTIVE_SCORE = (
        f"CASE WHEN {_LOCATION_MISMATCH} AND COALESCE(m.score, t.score) > {LOCATION_MISMATCH_CAP}"
        f" THEN {LOCATION_MISMATCH_CAP} ELSE COALESCE(m.score, t.score) END"
    )

    _CURRENT_SCORES = """
        LEFT JOIN scores t ON t.id = (SELECT id FROM scores WHERE job_id = j.id
            AND stage = 'triage' AND content_hash = j.content_hash ORDER BY id DESC LIMIT 1)
        LEFT JOIN scores m ON m.id = (SELECT id FROM scores WHERE job_id = j.id
            AND stage = 'match' AND content_hash = j.content_hash
            ORDER BY rubric_version DESC, id DESC LIMIT 1)
        LEFT JOIN reviews r ON r.id = (
            SELECT id FROM reviews WHERE job_id = j.id ORDER BY id DESC LIMIT 1)
        LEFT JOIN job_decisions d ON d.id = (
            SELECT id FROM job_decisions WHERE job_id = j.id ORDER BY id DESC LIMIT 1)
        LEFT JOIN job_views v ON v.job_id = j.id
    """

    # Dismissed is permanent and attached to the job, so it is excluded in SQL everywhere a
    # list is built, never filtered by the client.
    _NOT_DISMISSED = "COALESCE(d.decision, '') <> 'dismissed'"

    # What the lifecycle adds to a row. The CV version is looked up from the hash the score
    # recorded; it is NULL for a score from before CVs were versioned.
    _LIFECYCLE_COLUMNS = """
        d.decision AS decision, v.viewed_at AS viewed_at,
        (SELECT id FROM applications WHERE job_id = j.id) AS notion_index,
        (SELECT version FROM cv_versions WHERE sha256 = m.cv_sha256 LIMIT 1) AS scored_with_cv
    """

    # New or saved sort into one top tier. A new posting only pins when it is a real
    # candidate (rules kept it and a model scored it): a rule-rejected posting is new to the
    # user but it is not a match, and must not float above the ones that are.
    # An applied posting is done with: it stays listed but sorts below new and saved.
    _PINNED = (
        "CASE WHEN d.decision = 'applied' THEN 0 WHEN d.decision = 'saved' THEN 1 "
        "WHEN v.job_id IS NULL "
        "AND j.filter_outcome IN ('pass', 'manual_review') "
        "AND COALESCE(m.score, t.score) IS NOT NULL THEN 1 ELSE 0 END"
    )

    # Rules-kept: the deterministic rules did not drop it.
    _RULES_KEPT = "j.filter_outcome IN ('pass', 'manual_review')"

    # Kept: rules-kept AND the model scored it at or above the threshold. An unscored
    # posting is not kept: it has no score to clear the bar with, so the default view
    # leaves it out and says how many there are instead of passing it off as relevant.
    _KEPT = f"{_RULES_KEPT} AND {_EFFECTIVE_SCORE} >= ?"

    # The most rows one request may return, in either mode.
    REVIEW_MAX_LIMIT = 50

    def review_page(
        self,
        threshold: int,
        include_below: bool = False,
        limit: int = 10,
        decision: str | None = None,
    ) -> dict[str, object]:
        """The ranked list behind `/review`, with the tally of what the cut hides.

        By default only scored postings at or above the threshold. `include_below` also
        reveals below-threshold, unscored and rule-rejected postings, still ranked. Either
        way at most `REVIEW_MAX_LIMIT` rows come back.

        Dismissed postings never appear. Saved ones always do, whatever the threshold: they
        are the user's apply list. New and saved postings sort first, each tier by score.
        `decision` narrows the list to one current decision (the saved-jobs filter).

        Not `match_scores`: that returns only postings with a current match score, which
        is one row of 1071 in a database where scoring has barely run. The list has to be
        able to say "the rules kept this, the model has not looked at it yet".
        """
        limit = max(1, min(limit, self.REVIEW_MAX_LIMIT))
        where = f"j.status = '{JobStatus.ACTIVE}' AND {self._NOT_DISMISSED}"

        def count(extra: str, *args: Any) -> int:
            row = self._conn.execute(
                f"SELECT COUNT(*) FROM jobs j {self._CURRENT_SCORES} WHERE {where} AND {extra}",
                args,
            ).fetchone()
            return int(row[0])

        kept = count(self._KEPT, threshold)
        below = count(f"{self._RULES_KEPT} AND {self._EFFECTIVE_SCORE} < ?", threshold)
        unscored = count(f"{self._RULES_KEPT} AND {self._EFFECTIVE_SCORE} IS NULL")
        total = count("1 = 1")
        visible = (
            where
            if include_below
            else f"{where} AND ({self._KEPT} OR d.decision IN ('saved', 'applied'))"
        )
        args: list[Any] = [] if include_below else [threshold]
        if decision is not None:
            visible += " AND d.decision = ?"
            args.append(decision)
        rows = self._conn.execute(
            f"""SELECT j.*, t.score AS triage_score, t.reasons AS triage_reasons,
                    m.score AS match_score, m.verdict AS match_verdict, m.reasons AS match_reasons,
                    m.parts AS match_parts,
                    r.label AS review_label, r.content_hash AS review_content_hash,
                    {self._EFFECTIVE_SCORE} AS effective_score,
                    {self._LIFECYCLE_COLUMNS}
                FROM jobs j {self._CURRENT_SCORES}
                WHERE {visible}
                ORDER BY {self._PINNED} DESC, effective_score DESC NULLS LAST,
                    j.last_seen_at DESC, j.id DESC
                LIMIT ?""",
            [*args, limit],
        )
        items = [self._review_row(row, threshold) for row in rows]
        return {
            "items": items,
            "threshold": threshold,
            "limit": limit,
            "kept": kept,
            "dropped": total - kept,
            "below": below,
            "unscored": unscored,
            "total": total,
        }

    def listed_titles(self, threshold: int, include_below: bool) -> list[tuple[int, str]]:
        """(id, title) of every posting the page lists, saved and applied ones left out.

        `review_page` bounds its rows; a bulk change has to see all of them. Saved and
        applied postings are the user's own apply list, so a bulk change never touches them.
        """
        where = (
            f"j.status = '{JobStatus.ACTIVE}' AND {self._NOT_DISMISSED}"
            " AND COALESCE(d.decision, '') NOT IN ('saved', 'applied')"
        )
        args: list[Any] = []
        if not include_below:
            where += f" AND {self._KEPT}"
            args.append(threshold)
        rows = self._conn.execute(
            f"SELECT j.id, j.title FROM jobs j {self._CURRENT_SCORES} WHERE {where} ORDER BY j.id",
            args,
        )
        return [(int(row["id"]), str(row["title"])) for row in rows]

    def _review_row(self, row: sqlite3.Row, threshold: int) -> dict[str, object]:
        match_reasons = _split(row["match_reasons"] or "")
        triage_reasons = _split(row["triage_reasons"] or "")
        filter_reasons = _split(row["filter_reasons"])
        score = row["effective_score"]
        return {
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "source": row["source"],
            "url": row["canonical_url"],
            "firstSeenAt": row["first_seen_at"],
            # When the crawler last saw the advert on its board. A posting the board has
            # dropped still sits here until `inactive_after_days` retires it, and clicking
            # it lands on the board's own "no longer available" page — so the screen needs
            # this to warn before the click, not after.
            "lastSeenAt": row["last_seen_at"],
            "outcome": row["filter_outcome"],
            "score": score,
            # Which pass produced the score above, so the badge can say NOT CALLED rather
            # than inventing a verdict for a posting no model has read.
            "scoreStage": ("match" if row["match_score"] is not None else "triage")
            if score is not None
            else None,
            "verdict": row["match_verdict"],
            # The four named parts behind the match score. None for a score from before
            # breakdowns existed (rubric 1) and for a posting with no match score yet.
            "parts": json.loads(row["match_parts"]) if row["match_parts"] else None,
            # One line, in the order of how much it was paid for.
            "why": next(iter(match_reasons + triage_reasons + filter_reasons), ""),
            "label": row["review_label"],
            # True once the advert changed under a label the user already gave.
            "labelStale": bool(
                row["review_label"] and row["review_content_hash"] != row["content_hash"]
            ),
            "belowThreshold": score is not None and score < threshold,
            "locationMismatch": any(
                reason.startswith(("remote role with non-German location", "location outside"))
                for reason in (*_split(row["filter_warnings"]), *filter_reasons)
            ),
            # `restored` counts as no decision: the job is back as it was before dismissal.
            "decision": None if row["decision"] == "restored" else row["decision"],
            "isSaved": row["decision"] == "saved",
            "isApplied": row["decision"] == "applied",
            "notionIndex": row["notion_index"],
            "isNew": row["viewed_at"] is None,
            "viewedAt": row["viewed_at"],
            "scoredWithCv": row["scored_with_cv"],
        }

    def job_detail(self, job_id: int) -> dict[str, object] | None:
        """One card. Everything the screen shows about a posting, in one read."""
        row = self._conn.execute(
            f"""SELECT j.*, t.score AS triage_score, t.reasons AS triage_reasons,
                    t.cost_eur AS triage_cost,
                    m.score AS match_score, m.verdict AS match_verdict, m.reasons AS match_reasons,
                    m.gaps AS match_gaps, m.warnings AS match_warnings,
                    m.evidence_refs AS match_evidence, m.rubric_version AS match_rubric,
                    m.model AS match_model, m.cost_eur AS match_cost,
                    m.created_at AS match_created_at, m.parts AS match_parts,
                    r.label AS review_label, r.note AS review_note,
                    r.content_hash AS review_content_hash, r.created_at AS review_created_at,
                    {self._EFFECTIVE_SCORE} AS effective_score,
                    {self._LIFECYCLE_COLUMNS}
                FROM jobs j {self._CURRENT_SCORES}
                WHERE j.id = ?""",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        detail = self._review_row(row, threshold=0)
        detail["belowThreshold"] = False
        detail.update(
            {
                "description": row["description"],
                "employmentType": row["employment_type"],
                "remote": bool(row["remote"]),
                "lastSeenAt": row["last_seen_at"],
                "contentHash": row["content_hash"],
                "filterReasons": list(_split(row["filter_reasons"])),
                "filterWarnings": list(_split(row["filter_warnings"])),
                "triageScore": row["triage_score"],
                "triageReasons": list(_split(row["triage_reasons"] or "")),
                "matchScore": row["match_score"],
                "reasons": list(_split(row["match_reasons"] or "")),
                "gaps": list(_split(row["match_gaps"] or "")),
                "warnings": list(_split(row["match_warnings"] or "")),
                # The verbatim resume quote behind each claim, in claim order.
                "evidence": list(_split(row["match_evidence"] or "")),
                "rubricVersion": row["match_rubric"],
                "model": row["match_model"],
                "scoredAt": row["match_created_at"],
                "costEur": (row["triage_cost"] or 0) + (row["match_cost"] or 0)
                if row["triage_cost"] is not None or row["match_cost"] is not None
                else None,
                "note": row["review_note"],
                "labelledAt": row["review_created_at"],
            }
        )
        return detail

    def record_review(self, job_id: int, label: str, note: str, now: datetime) -> int:
        """Append one label. Never an update: the previous opinion stays on the record."""
        # A cleaned job is gone for good; a label's decision must not bring it back.
        content_hash = self._conn.execute(
            "SELECT content_hash FROM jobs WHERE id = ? AND tombstoned_at IS NULL", (job_id,)
        ).fetchone()
        if content_hash is None:
            raise LookupError(f"no job {job_id}")
        cur = self._conn.execute(
            "INSERT INTO reviews (job_id, label, note, content_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, label, note, content_hash[0], now.isoformat()),
        )
        # The label's decision is written in the same transaction, so "is this hidden" has
        # one answer however the user got there.
        mapped = LABEL_TO_DECISION.get(label)
        if mapped is not None:
            self._write_decision(job_id, mapped, note, now)
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def record_decision(
        self, job_id: int, decision: str, note: str, now: datetime, reason: str | None = None
    ) -> int:
        """Append one decision. Never an update: the log is what a later lifecycle replays."""
        # A cleaned job is gone for good; no decision can bring it back.
        if (
            self._conn.execute(
                "SELECT 1 FROM jobs WHERE id = ? AND tombstoned_at IS NULL", (job_id,)
            ).fetchone()
            is None
        ):
            raise LookupError(f"no job {job_id}")
        decision_id = self._write_decision(job_id, decision, note, now, reason)
        self._conn.commit()
        return decision_id

    def undo_applied(self, job_id: int, now: datetime) -> str | None:
        """Appends the decision that stood before Applied, so undoing is a round trip.

        Returns that decision, or None when there was none (`restored` is appended: like a
        job back from the bin, it counts as no decision).
        Raises LookupError for an unknown job and ValueError when the job is not applied.
        """
        if self._conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
            raise LookupError(f"no job {job_id}")
        history = [
            row[0]
            for row in self._conn.execute(
                "SELECT decision FROM job_decisions WHERE job_id = ? ORDER BY id DESC", (job_id,)
            )
        ]
        if not history or history[0] != "applied":
            raise ValueError(f"job {job_id} is not applied")
        before = next((d for d in history if d != "applied"), "restored")
        self._write_decision(job_id, before, "", now)
        self._conn.commit()
        return None if before == "restored" else before

    def _write_decision(
        self, job_id: int, decision: str, note: str, now: datetime, reason: str | None = None
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO job_decisions (job_id, decision, note, reason, created_at)"
            " VALUES (?,?,?,?,?)",
            (job_id, decision, note or None, reason, now.isoformat()),
        )
        # Deciding on a posting implies the user saw it.
        self._conn.execute(
            "INSERT OR IGNORE INTO job_views (job_id, viewed_at) VALUES (?, ?)",
            (job_id, now.isoformat()),
        )
        return int(cur.lastrowid or 0)

    def decision_signals(self, limit: int = 40) -> Sequence[DecisionSignal]:
        """The newest saved and dismissed jobs, each with its latest dismissal reason.

        A repost hidden by the repost check is left out: the user decided once, and the
        copy would count the same choice twice.
        """
        rows = self._conn.execute(
            """
            SELECT j.title, j.company, d.decision,
                   (SELECT r.reason FROM job_decisions r
                    WHERE r.job_id = j.id AND r.decision = 'dismissed' AND r.reason IS NOT NULL
                    ORDER BY r.id DESC LIMIT 1) AS reason
            FROM jobs j
            JOIN job_decisions d ON d.id = (
                SELECT id FROM job_decisions WHERE job_id = j.id ORDER BY id DESC LIMIT 1)
            WHERE d.decision IN ('saved', 'dismissed') AND j.repost_of IS NULL
            ORDER BY d.id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            DecisionSignal(row["decision"], row["title"], row["company"], row["reason"])
            for row in rows
        ]

    def get_job(self, job_id: int) -> StoredJob | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._to_stored(row) if row is not None else None

    def get_outline(self, job_id: int) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        """The saved responsibilities and requirements, if they were read from the posting as
        it is now. A posting that changed since is read again, so this returns None for it."""
        row = self._conn.execute(
            """SELECT o.responsibilities, o.requirements
               FROM job_outlines o JOIN jobs j ON j.id = o.job_id
               WHERE o.job_id = ? AND o.content_hash = j.content_hash""",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return tuple(json.loads(row[0])), tuple(json.loads(row[1]))

    def save_outline(
        self,
        job_id: int,
        content_hash: str,
        responsibilities: Sequence[str],
        requirements: Sequence[str],
        model: str,
        now: datetime,
    ) -> None:
        self._conn.execute(
            """INSERT OR REPLACE INTO job_outlines
               (job_id, content_hash, responsibilities, requirements, model, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                content_hash,
                json.dumps(list(responsibilities)),
                json.dumps(list(requirements)),
                model,
                now.isoformat(),
            ),
        )
        self._conn.commit()

    def dismissed_siblings(self, job_id: int) -> Sequence[StoredJob]:
        """Dismissed jobs of the same company. A job that was restored is not dismissed."""
        rows = self._conn.execute(
            """
            SELECT j.* FROM jobs j
            LEFT JOIN job_decisions d ON d.id = (
                SELECT id FROM job_decisions WHERE job_id = j.id ORDER BY id DESC LIMIT 1)
            WHERE j.company_norm = (SELECT company_norm FROM jobs WHERE id = ?)
              AND j.id <> ? AND d.decision = 'dismissed'
            ORDER BY j.id
            """,
            (job_id, job_id),
        )
        return [self._to_stored(row) for row in rows]

    def is_cleaned(self, job_id: int) -> bool:
        row = self._conn.execute(
            "SELECT tombstoned_at FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        return row is not None and row[0] is not None

    def hide_as_repost(self, job_id: int, of_job_id: int, now: datetime) -> None:
        """Dismiss a repost the way its original was dismissed, and remember which."""
        self._conn.execute("UPDATE jobs SET repost_of = ? WHERE id = ?", (of_job_id, job_id))
        self._write_decision(job_id, "dismissed", f"repost of {of_job_id}", now)
        self._conn.commit()

    def repost_of(self, job_id: int) -> int | None:
        row = self._conn.execute("SELECT repost_of FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def mark_viewed(self, job_ids: Sequence[int], now: datetime) -> int:
        """Record the first time each job was shown. Idempotent; unknown ids are ignored.
        Returns how many were newly marked."""
        marked = 0
        for job_id in dict.fromkeys(job_ids):
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO job_views (job_id, viewed_at) "
                "SELECT id, ? FROM jobs WHERE id = ?",
                (now.isoformat(), job_id),
            )
            marked += cur.rowcount
        self._conn.commit()
        return marked

    def record_correction(
        self, job_id: int | None, kind: str, target: str, note: str, now: datetime
    ) -> int:
        """Append one dispute. `job_id` is None for a note about the batch."""
        if job_id is not None and (
            self._conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None
        ):
            raise LookupError(f"no job {job_id}")
        cur = self._conn.execute(
            "INSERT INTO corrections (job_id, kind, target, note, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, kind, target, note, now.isoformat()),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def all_active_jobs(self) -> Sequence[StoredJob]:
        """Every active job, unbounded. `list_jobs` is a display query with a limit; a
        re-filter has to see the whole table or it would silently skip rows."""
        rows = self._conn.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id",
            (str(JobStatus.ACTIVE),),
        )
        return [self._to_stored(row) for row in rows]

    def update_filter_result(self, job_id: int, verdict: HardFilterResult) -> None:
        """Re-record a job's deterministic outcome. Never touches `needs_rescore`: that
        flag means "the posting changed", and preference edits are tracked by the
        `preferences_hash` on the score row instead."""
        self._conn.execute(
            """
            UPDATE jobs SET filter_outcome = ?, filter_reasons = ?, filter_warnings = ?
            WHERE id = ?
            """,
            (str(verdict.outcome), _join(verdict.reasons), _join(verdict.warnings), job_id),
        )
        if not self._in_transaction:
            self._conn.commit()

    def has_jobs(self) -> bool:
        """False only on a never-run database. Decides the initial discovery target."""
        return self._conn.execute("SELECT 1 FROM jobs LIMIT 1").fetchone() is not None

    def mark_inactive_before(self, cutoff: datetime) -> int:
        """Jobs unseen since the cutoff go inactive. Nothing is ever deleted."""
        cur = self._conn.execute(
            "UPDATE jobs SET status = ? WHERE status = ? AND last_seen_at < ?",
            (str(JobStatus.INACTIVE), str(JobStatus.ACTIVE), cutoff.isoformat()),
        )
        self._conn.commit()
        return cur.rowcount

    @staticmethod
    def _to_stored(row: sqlite3.Row) -> StoredJob:
        posting = JobPosting(
            source=row["source"],
            source_job_id=row["source_job_id"],
            company=row["company"],
            company_norm=row["company_norm"],
            title=row["title"],
            title_norm=row["title_norm"],
            location=row["location"],
            location_norm=row["location_norm"],
            canonical_url=row["canonical_url"],
            description=row["description"],
            fingerprint=row["fingerprint"],
            content_hash=row["content_hash"],
            posted_at=_dt(row["posted_at"]),
            employment_type=row["employment_type"],
            remote=bool(row["remote"]),
        )
        return StoredJob(
            id=int(row["id"]),
            posting=posting,
            filter_outcome=FilterOutcome(row["filter_outcome"]),
            filter_reasons=_split(row["filter_reasons"]),
            filter_warnings=_split(row["filter_warnings"]),
            status=JobStatus(row["status"]),
            first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        )

    # --- scores -----------------------------------------------------------

    def has_match_scores(self) -> bool:
        """True once any match score exists. Decides initial versus incremental mode."""
        row = self._conn.execute(
            "SELECT 1 FROM scores WHERE stage = ? LIMIT 1", (str(ScoreStage.MATCH),)
        ).fetchone()
        return row is not None

    def jobs_needing_score(
        self,
        model: str,
        rubric_version: int,
        limit: int | None = None,
        preferences_hash: str = "",
    ) -> Sequence[StoredJob]:
        """Active jobs that passed the hard filters and have no current match score.

        `content_hash`, `rubric_version`, `model` and `preferences_hash` all take part, so
        a changed posting, a rubric bump, a model swap, or an edit to a prompt-affecting
        preference each re-open the job. A new CV does not, and neither does a dismissed or
        applied job: paying to score a posting the user has hidden or acted on buys nothing.
        """
        sql = """
            SELECT j.* FROM jobs j
            WHERE j.status = ? AND j.filter_outcome = ?
              AND COALESCE((SELECT decision FROM job_decisions WHERE job_id = j.id
                            ORDER BY id DESC LIMIT 1), '') NOT IN ('dismissed', 'applied')
              AND NOT EXISTS (
                  SELECT 1 FROM scores s
                  WHERE s.job_id = j.id AND s.stage = ?
                    AND s.content_hash = j.content_hash
                    AND s.rubric_version = ? AND s.model = ?
                    AND s.preferences_hash = ?
              )
            ORDER BY j.last_seen_at DESC, j.id DESC
        """
        args: list[Any] = [
            str(JobStatus.ACTIVE),
            str(FilterOutcome.PASS),
            str(ScoreStage.MATCH),
            rubric_version,
            model,
            preferences_hash,
        ]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self._conn.execute(sql, args)
        return [self._to_stored(row) for row in rows]

    def record_score(self, score: Score, now: datetime, cost_eur: float | None = None) -> int:
        """Insert a score. Returns 0 when this exact score already exists."""
        cur = self._conn.execute(
            """
            INSERT OR IGNORE INTO scores (
                job_id, stage, score, verdict, reasons, gaps, warnings, evidence_refs,
                rubric_version, model, prompt_hash, content_hash, created_at,
                preferences_version, preferences_hash, cost_eur, cv_sha256, parts
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                score.job_id,
                str(score.stage),
                score.score,
                str(score.verdict),
                _join(score.reasons),
                _join(score.gaps),
                _join(score.warnings),
                _join(score.evidence_refs),
                score.rubric_version,
                score.model,
                score.prompt_hash,
                score.content_hash,
                now.isoformat(),
                score.preferences_version,
                score.preferences_hash,
                cost_eur,
                score.cv_sha256,
                json.dumps(score.parts) if score.parts is not None else None,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0) if cur.rowcount else 0

    def match_scores(
        self,
        limit: int | None = 50,
        model: str | None = None,
        rubric_version: int | None = None,
        max_per_company: int | None = None,
        preferences_hash: str | None = None,
    ) -> Sequence[ScoredJob]:
        """Current match score per active, still-qualified job, for the digest.

        `max_per_company` drops a company's surplus rows inside SQL, so `limit`
        still bounds the result set after the diversity cap has been applied.
        """
        base = """
            SELECT j.*, s.score AS s_score, s.verdict AS s_verdict, s.reasons AS s_reasons,
                   s.gaps AS s_gaps, s.warnings AS s_warnings,
                   s.evidence_refs AS s_evidence, s.rubric_version AS s_rubric,
                   s.model AS s_model, s.prompt_hash AS s_prompt_hash,
                   s.content_hash AS s_content_hash, s.created_at AS s_created_at,
                   s.preferences_version AS s_prefs_version,
                   s.preferences_hash AS s_prefs_hash, s.parts AS s_parts,
                   ROW_NUMBER() OVER (
                       PARTITION BY j.company_norm
                       ORDER BY s.score DESC, j.last_seen_at DESC, j.id DESC
                   ) AS company_rank
            FROM jobs j
            JOIN scores s ON s.id = (
                SELECT id FROM scores
                WHERE job_id = j.id AND stage = ? AND content_hash = j.content_hash
                  AND (? IS NULL OR model = ?)
                  AND (? IS NULL OR rubric_version = ?)
                  AND (? IS NULL OR preferences_hash = ?)
                ORDER BY rubric_version DESC, created_at DESC, id DESC LIMIT 1
            )
            WHERE j.status = ? AND j.filter_outcome = ?
              AND COALESCE((SELECT decision FROM job_decisions WHERE job_id = j.id
                            ORDER BY id DESC LIMIT 1), '') <> 'dismissed'
        """
        args: list[Any] = [
            str(ScoreStage.MATCH),
            model,
            model,
            rubric_version,
            rubric_version,
            preferences_hash,
            preferences_hash,
            str(JobStatus.ACTIVE),
            str(FilterOutcome.PASS),
        ]
        sql = f"SELECT * FROM ({base}) WHERE (? IS NULL OR company_rank <= ?)"
        args += [max_per_company, max_per_company]
        sql += " ORDER BY s_score DESC, last_seen_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self._conn.execute(sql, args)
        return [
            ScoredJob(
                job=self._to_stored(row),
                score=Score(
                    job_id=int(row["id"]),
                    stage=ScoreStage.MATCH,
                    score=int(row["s_score"]),
                    verdict=ScoreVerdict(row["s_verdict"]),
                    rubric_version=int(row["s_rubric"]),
                    model=row["s_model"],
                    prompt_hash=row["s_prompt_hash"],
                    content_hash=row["s_content_hash"],
                    reasons=_split(row["s_reasons"]),
                    gaps=_split(row["s_gaps"]),
                    warnings=_split(row["s_warnings"]),
                    evidence_refs=_split(row["s_evidence"]),
                    created_at=row["s_created_at"],
                    preferences_version=int(row["s_prefs_version"]),
                    preferences_hash=row["s_prefs_hash"],
                    parts=json.loads(row["s_parts"]) if row["s_parts"] else None,
                ),
            )
            for row in rows
        ]

    def triage_scores(self, limit: int | None = None) -> Sequence[ScoredJob]:
        """Latest current triage score for each active hard-filter survivor."""
        sql = """
            SELECT j.*, s.score AS s_score, s.verdict AS s_verdict, s.reasons AS s_reasons,
                   s.rubric_version AS s_rubric, s.model AS s_model,
                   s.prompt_hash AS s_prompt_hash, s.content_hash AS s_content_hash,
                   s.created_at AS s_created_at
            FROM jobs j
            JOIN scores s ON s.id = (
                SELECT id FROM scores
                WHERE job_id = j.id AND stage = ? AND content_hash = j.content_hash
                ORDER BY created_at DESC, id DESC LIMIT 1
            )
            WHERE j.status = ? AND j.filter_outcome = ?
            ORDER BY s.score DESC, j.id ASC
        """
        args: list[Any] = [str(ScoreStage.TRIAGE), str(JobStatus.ACTIVE), str(FilterOutcome.PASS)]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = self._conn.execute(sql, args)
        return [
            ScoredJob(
                job=self._to_stored(row),
                score=Score(
                    job_id=int(row["id"]),
                    stage=ScoreStage.TRIAGE,
                    score=int(row["s_score"]),
                    verdict=ScoreVerdict(row["s_verdict"]),
                    rubric_version=int(row["s_rubric"]),
                    model=row["s_model"],
                    prompt_hash=row["s_prompt_hash"],
                    content_hash=row["s_content_hash"],
                    reasons=_split(row["s_reasons"]),
                    created_at=row["s_created_at"],
                ),
            )
            for row in rows
        ]

    def digest_stats(
        self,
        model: str | None = None,
        rubric_version: int | None = None,
        preferences_hash: str | None = None,
    ) -> DigestStats:
        counts = self._conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = ? THEN 1 ELSE 0 END) AS active_jobs,
                SUM(CASE WHEN status = ? AND filter_outcome = ? THEN 1 ELSE 0 END)
                    AS manual_review
            FROM jobs
            """,
            (
                str(JobStatus.ACTIVE),
                str(JobStatus.ACTIVE),
                str(FilterOutcome.MANUAL_REVIEW),
            ),
        ).fetchone()
        scored = self._conn.execute(
            """
            SELECT COUNT(DISTINCT j.id) AS n
            FROM jobs j JOIN scores s ON s.job_id = j.id
            WHERE j.status = ? AND j.filter_outcome = ? AND s.stage = ?
              AND s.content_hash = j.content_hash
              AND (? IS NULL OR s.model = ?)
              AND (? IS NULL OR s.rubric_version = ?)
              AND (? IS NULL OR s.preferences_hash = ?)
            """,
            (
                str(JobStatus.ACTIVE),
                str(FilterOutcome.PASS),
                str(ScoreStage.MATCH),
                model,
                model,
                rubric_version,
                rubric_version,
                preferences_hash,
                preferences_hash,
            ),
        ).fetchone()
        failures = self._conn.execute(
            """
            SELECT source, identifier, error FROM source_runs
            WHERE run_id = (SELECT MAX(run_id) FROM source_runs) AND ok = 0
            ORDER BY source, identifier
            """
        ).fetchall()
        watchlist = self.list_import_companies(
            statuses=(ImportStatus.UNRESOLVED, ImportStatus.BLOCKED)
        )
        return DigestStats(
            active_jobs=int(counts["active_jobs"] or 0),
            scored_jobs=int(scored["n"] or 0),
            manual_review=int(counts["manual_review"] or 0),
            failed_sources=tuple(
                f"{row['source']}:{row['identifier']} — {row['error'] or 'unknown error'}"
                for row in failures
            ),
            watchlist_unresolved=tuple(
                c.name for c in watchlist if c.status is ImportStatus.UNRESOLVED
            ),
            watchlist_blocked=tuple(
                (c.name, c.last_error or "blocked")
                for c in watchlist
                if c.status is ImportStatus.BLOCKED
            ),
        )

    def clear_needs_rescore(self, job_id: int) -> None:
        self._conn.execute("UPDATE jobs SET needs_rescore = 0 WHERE id = ?", (job_id,))
        self._conn.commit()

    def record_run_cost(self, run_id: int, calls: int, cost_eur: float) -> None:
        self._conn.execute(
            "UPDATE runs SET llm_calls = ?, cost_eur = ? WHERE id = ?", (calls, cost_eur, run_id)
        )
        self._conn.commit()

    # --- preferences ------------------------------------------------------

    def load_current(self) -> Preferences | None:
        """The newest saved version, or None on a database that has never saved any.

        None is meaningful: it is what makes the caller fall back to the in-memory
        bootstrap at version 0, rather than this method inventing one.
        """
        row = self._conn.execute(
            "SELECT * FROM preferences ORDER BY version DESC LIMIT 1"
        ).fetchone()
        return self._to_preferences(row)

    def load_version(self, version: int) -> Preferences | None:
        """One historical version, so a score row can be read back against the
        preferences that actually produced it."""
        row = self._conn.execute(
            "SELECT * FROM preferences WHERE version = ?", (version,)
        ).fetchone()
        return self._to_preferences(row)

    def save(self, prefs: Preferences, now: datetime) -> Preferences:
        """Append a new version. Nothing is ever updated in place: a score row names the
        version that produced it, so an edited row would rewrite history."""
        # The profile is attached per run from its own table; it is never form state.
        prefs = replace(prefs, search_profile="")
        cur = self._conn.execute(
            "INSERT INTO preferences (payload, prompt_hash, created_at) VALUES (?,?,?)",
            (json.dumps(prefs.to_payload()), prefs.prompt_hash(), now.isoformat()),
        )
        if not self._in_transaction:
            self._conn.commit()
        return replace(prefs, version=int(cur.lastrowid or 0), updated_at=now)

    @staticmethod
    def _to_preferences(row: sqlite3.Row | None) -> Preferences | None:
        if row is None:
            return None
        return Preferences.from_payload(
            json.loads(row["payload"]),
            version=int(row["version"]),
            updated_at=datetime.fromisoformat(row["created_at"]),
        )

    # --- model settings ---------------------------------------------------

    def model_settings(self) -> ModelSettings | None:
        """The newest saved model settings, or None when the user never chose any."""
        row = self._conn.execute(
            "SELECT * FROM model_settings ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return ModelSettings(
            version=int(row["version"]),
            preset=parse_preset(row["preset"]),
            overrides=parse_overrides(json.loads(row["overrides"])),
            cloud_consent=bool(row["cloud_consent"]),
            saved_at=datetime.fromisoformat(row["saved_at"]),
        )

    def save_model_settings(self, settings: ModelSettings, now: datetime) -> ModelSettings:
        """Append a version. Rows are never updated: the history says what ran when."""
        overrides = {role: o.to_payload() for role, o in settings.overrides.items()}
        cur = self._conn.execute(
            "INSERT INTO model_settings (preset, overrides, cloud_consent, saved_at)"
            " VALUES (?,?,?,?)",
            (settings.preset, json.dumps(overrides), int(settings.cloud_consent), now.isoformat()),
        )
        if not self._in_transaction:
            self._conn.commit()
        return replace(settings, version=int(cur.lastrowid or 0), saved_at=now)

    # --- UI flags ---------------------------------------------------------

    def ui_flag(self, name: str) -> bool | None:
        """A remembered yes/no choice, or None when the user never made it."""
        row = self._conn.execute("SELECT value FROM ui_flags WHERE name = ?", (name,)).fetchone()
        return None if row is None else bool(row["value"])

    def set_ui_flag(self, name: str, value: bool) -> None:
        self._conn.execute(
            "INSERT INTO ui_flags (name, value) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, int(value)),
        )
        if not self._in_transaction:
            self._conn.commit()

    # --- CV versions ------------------------------------------------------

    def save_cv(self, filename: str, text: str, now: datetime) -> SavedCv:
        """Store an upload. The same normalized text is a no-op, or a revert when it matches
        an older version; different text becomes the next version and the current one."""
        digest = cv_sha256(text)
        with nullcontext() if self._in_transaction else self.transaction():
            existing = self._conn.execute(
                "SELECT * FROM cv_versions WHERE user_id IS NULL AND sha256 = ?", (digest,)
            ).fetchone()
            if existing is not None and existing["is_current"]:
                return SavedCv(self._to_cv(existing), changed=False)
            self._conn.execute("UPDATE cv_versions SET is_current = 0 WHERE user_id IS NULL")
            if existing is not None:
                self._conn.execute(
                    "UPDATE cv_versions SET is_current = 1 WHERE id = ?", (existing["id"],)
                )
                row_id = int(existing["id"])
            else:
                version = self._conn.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 FROM cv_versions WHERE user_id IS NULL"
                ).fetchone()[0]
                cur = self._conn.execute(
                    "INSERT INTO cv_versions (version, sha256, filename, text, uploaded_at) "
                    "VALUES (?,?,?,?,?)",
                    (version, digest, filename, text, now.isoformat()),
                )
                row_id = int(cur.lastrowid or 0)
            row = self._conn.execute("SELECT * FROM cv_versions WHERE id = ?", (row_id,)).fetchone()
        return SavedCv(self._to_cv(row), changed=True)

    def current_cv(self) -> StoredCv | None:
        row = self._conn.execute(
            "SELECT * FROM cv_versions WHERE user_id IS NULL AND is_current = 1"
        ).fetchone()
        return self._to_cv(row) if row is not None else None

    def list_cvs(self) -> Sequence[StoredCv]:
        rows = self._conn.execute(
            "SELECT * FROM cv_versions WHERE user_id IS NULL ORDER BY version DESC"
        ).fetchall()
        return [self._to_cv(row) for row in rows]

    def delete_cvs(self) -> int:
        """Delete every stored version. Scores keep their `cv_sha256`; a hash is not a CV."""
        cur = self._conn.execute("DELETE FROM cv_versions WHERE user_id IS NULL")
        if not self._in_transaction:
            self._conn.commit()
        return cur.rowcount

    @staticmethod
    def _to_cv(row: sqlite3.Row) -> StoredCv:
        return StoredCv(
            version=int(row["version"]),
            sha256=row["sha256"],
            filename=row["filename"],
            text=row["text"],
            uploaded_at=datetime.fromisoformat(row["uploaded_at"]),
            current=bool(row["is_current"]),
        )

    # --- intent log -------------------------------------------------------

    def add_intent(
        self, text: str, now: datetime, kind: IntentKind = IntentKind.ENTRY
    ) -> IntentEntry:
        """Append an entry. Nothing is edited in place: a refinement is a newer entry."""
        cur = self._conn.execute(
            "INSERT INTO intent_entries (text, kind, created_at) VALUES (?,?,?)",
            (text, kind.value, now.isoformat()),
        )
        if not self._in_transaction:
            self._conn.commit()
        return IntentEntry(int(cur.lastrowid or 0), text, kind, now)

    def active_intent(self) -> list[IntentEntry]:
        """The entries the agent reads, oldest first, so a later one can refine an earlier."""
        rows = self._conn.execute(
            "SELECT * FROM intent_entries WHERE user_id IS NULL AND archived_at IS NULL ORDER BY id"
        ).fetchall()
        return [
            IntentEntry(
                int(row["id"]),
                row["text"],
                IntentKind(row["kind"]),
                datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def archive_intent(self, entry_id: int, now: datetime) -> bool:
        """Hide an entry from the agent. False when there is no active entry by that id."""
        cur = self._conn.execute(
            "UPDATE intent_entries SET archived_at = ? "
            "WHERE id = ? AND user_id IS NULL AND archived_at IS NULL",
            (now.isoformat(), entry_id),
        )
        if not self._in_transaction:
            self._conn.commit()
        return cur.rowcount == 1

    # --- search profiles --------------------------------------------------

    def search_profile(self, inputs_hash: str) -> SearchProfile | None:
        """The cached profile for exactly these inputs, if one was built."""
        row = self._conn.execute(
            "SELECT payload FROM search_profiles WHERE inputs_hash = ?", (inputs_hash,)
        ).fetchone()
        return SearchProfile.from_payload(json.loads(row["payload"])) if row else None

    def latest_search_profile(self) -> SearchProfile | None:
        """The newest profile, whatever it was built from: what a run starts with."""
        row = self._conn.execute(
            "SELECT payload FROM search_profiles ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return SearchProfile.from_payload(json.loads(row["payload"])) if row else None

    def save_search_profile(
        self, inputs_hash: str, profile: SearchProfile, model: str, now: datetime
    ) -> None:
        """Replacing on the same hash moves the row to the end, so it reads as latest."""
        self._conn.execute("DELETE FROM search_profiles WHERE inputs_hash = ?", (inputs_hash,))
        self._conn.execute(
            "INSERT INTO search_profiles (inputs_hash, payload, model, created_at) "
            "VALUES (?,?,?,?)",
            (inputs_hash, json.dumps(profile.to_payload()), model, now.isoformat()),
        )
        if not self._in_transaction:
            self._conn.commit()

    # --- job embeddings ---------------------------------------------------

    def job_vectors(self, model: str, text_hashes: Iterable[str]) -> dict[str, list[float]]:
        """Cached vectors for these posting texts under one model; misses are simply absent."""
        hashes = list(set(text_hashes))
        found: dict[str, list[float]] = {}
        for start in range(0, len(hashes), _VECTOR_CHUNK):  # stay under SQLite's bind limit
            chunk = hashes[start : start + _VECTOR_CHUNK]
            rows = self._conn.execute(
                "SELECT text_hash, vector FROM job_embeddings WHERE model = ? "
                f"AND text_hash IN ({','.join('?' * len(chunk))})",
                (model, *chunk),
            ).fetchall()
            for row in rows:
                found[row["text_hash"]] = _unpack_vector(row["vector"])
        return found

    def save_job_vectors(
        self, model: str, items: Iterable[tuple[str, Sequence[float]]], now: datetime
    ) -> None:
        """Store vectors as float32: half the bytes of float64, and the model's own precision."""
        self._conn.executemany(
            "INSERT OR REPLACE INTO job_embeddings (text_hash, model, dim, vector, created_at) "
            "VALUES (?,?,?,?,?)",
            [(h, model, len(v), _pack_vector(v), now.isoformat()) for h, v in items],
        )
        if not self._in_transaction:
            self._conn.commit()

    # --- run ledger -------------------------------------------------------

    def begin_run(
        self,
        kind: str,
        frozen: RunConfig,
        now: datetime,
        owner_pid: int,
        owner_started_at: str | None,
    ) -> int:
        """Reclaim and compete for the slot in one database transaction."""
        with nullcontext() if self._in_transaction else self.transaction():
            self._reclaim_dead_run(now)
            try:
                cur = self._conn.execute(
                    """INSERT INTO runs (
                        started_at, kind, state, budget_eur, mode, preferences_version,
                        preferences_hash, triage_model, match_model, rubric_version,
                        owner_pid, owner_started_at, heartbeat_at, config_json
                    ) VALUES (?,?,'running',?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        now.isoformat(),
                        kind,
                        frozen.budget_eur,
                        frozen.mode,
                        frozen.preferences_version,
                        frozen.preferences_hash,
                        frozen.triage_model,
                        frozen.match_model,
                        frozen.rubric_version,
                        owner_pid,
                        owner_started_at,
                        now.isoformat(),
                        json.dumps(frozen.payload()),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise RunInProgress("a run is already active") from exc
            return int(cur.lastrowid or 0)

    def run_config(self, run_id: int) -> RunConfig:
        row = self.get_run(run_id)
        if row is None:
            raise KeyError(run_id)
        data = json.loads(row["config_json"])
        prefs = Preferences.from_payload(
            data.pop("preferences"), version=data.pop("preferences_version")
        )
        return RunConfig(preferences=prefs, **data)

    def update_run_progress(self, run_id: int, progress: Progress, now: datetime) -> None:
        self._conn.execute(
            """UPDATE runs SET stage=?, completed=?, total=?, cost_eur=?, budget_eur=?,
               failed=?, heartbeat_at=? WHERE id=?
               AND (state IN ('running','syncing') OR ended_at IS NULL)""",
            (
                progress.stage,
                progress.completed,
                progress.total,
                progress.cost_eur,
                progress.budget_eur,
                progress.failed,
                now.isoformat(),
                run_id,
            ),
        )
        self._conn.commit()

    def _terminal(
        self, run_id: int, now: datetime, state: str, category: str, message: str
    ) -> None:
        self._conn.execute(
            """UPDATE runs SET state=?, ended_at=?, failure_category=?, failure_message=?
               WHERE id=? AND state IN ('running','syncing')""",
            (state, now.isoformat(), category, message, run_id),
        )
        if not self._in_transaction:
            self._conn.commit()

    def fail_run(self, run_id: int, now: datetime, category: str, message: str) -> None:
        self._terminal(run_id, now, "failed", category, message)

    def abort_run(self, run_id: int, now: datetime, reason: str) -> None:
        category = "shutdown" if reason == "shutdown" else "budget"
        self._terminal(run_id, now, "aborted", category, reason)

    def _reclaim_dead_run(self, now: datetime) -> int | None:
        row = self._conn.execute("SELECT * FROM runs WHERE state='running'").fetchone()
        if row is None or not stalled(row["heartbeat_at"], now):
            return None
        pid = row["owner_pid"]
        if pid == os.getpid():
            return None
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                return None  # cannot establish that the owner is gone
            else:
                identity = process_started_at(pid)
                if identity is None or identity == row["owner_started_at"]:
                    return None
        run_id = int(row["id"])
        self.fail_run(run_id, now, "interrupted", "The previous process stopped. Start a new run.")
        return run_id

    def reclaim_dead_run(self, now: datetime) -> int | None:
        with self.transaction():
            return self._reclaim_dead_run(now)

    def reclaim_stale_syncs(self, now: datetime) -> list[int]:
        """Fail `syncing` rows whose worker is gone. A sync heartbeats once per company, and
        a row that has not since it started (or last progressed) cannot be told apart from
        a dead one, so the same timeout as a pipeline run applies."""
        reclaimed: list[int] = []
        with self.transaction():
            rows = self._conn.execute(
                "SELECT id, heartbeat_at, started_at FROM runs WHERE state='syncing'"
            ).fetchall()
            for row in rows:
                if stalled(row["heartbeat_at"] or row["started_at"], now):
                    run_id = int(row["id"])
                    self.fail_run(
                        run_id, now, "interrupted", "The previous process stopped. Sync again."
                    )
                    reclaimed.append(run_id)
        return reclaimed

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["funnel"] = json.loads(result["funnel"]) if result.get("funnel") else None
        result["stages"] = [
            {
                "stage": s["stage"],
                "startedAt": s["started_at"],
                "endedAt": s["ended_at"],
                "seconds": _stage_seconds(s["started_at"], s["ended_at"]),
            }
            for s in self._conn.execute(
                "SELECT stage, started_at, ended_at FROM run_stages WHERE run_id = ? ORDER BY id",
                (run_id,),
            )
        ]
        result["source_failures"] = [
            {
                "source": r["source"],
                "identifier": r["identifier"],
                "message": "Source could not be fetched. Try again later.",
            }
            for r in self._conn.execute(
                "SELECT source, identifier FROM source_runs WHERE run_id=? AND ok=0", (run_id,)
            )
        ]
        if result["kind"] == "company_sync":
            # import_companies has no run_id of its own (it's a ledger, not a run log),
            # so a company_sync run records which list it synced (and its final counts)
            # in config_json, and its "failures" are that list's current
            # unresolved/blocked rows, read live. A legacy run with no recorded list
            # falls back to every list.
            meta = json.loads(result.get("config_json") or "{}")
            list_source = meta.get("list_source")
            result["company_counts"] = meta.get("counts")
            result["source_failures"].extend(
                {
                    "source": company.list_source,
                    "identifier": company.name,
                    "message": company.last_error or "no public board found",
                }
                for company in self.list_import_companies(
                    list_source, statuses=(ImportStatus.UNRESOLVED, ImportStatus.BLOCKED)
                )
            )
        return result

    def last_succeeded_run_at(self) -> datetime | None:
        """When the last scoring run finished cleanly: what "stale" is measured from."""
        row = self._conn.execute(
            "SELECT MAX(ended_at) FROM runs WHERE state = 'succeeded' AND kind IN ('full', 'score')"
        ).fetchone()
        return _dt(row[0])

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        ids = self._conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return [run for row in ids if (run := self.get_run(row["id"])) is not None]

    def start_run(self, now: datetime, kind: str = "discover") -> int:
        # A company sync is `syncing`, not `running`: the single-active-run index covers
        # only `running`, so a sync must not block (or be blocked by) a scoring run.
        state = "syncing" if kind == "company_sync" else "succeeded"
        cur = self._conn.execute(
            "INSERT INTO runs (started_at, kind, state) VALUES (?,?,?)",
            (now.isoformat(), kind, state),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def record_company_sync(
        self, run_id: int, list_source: str, counts: dict[str, int] | None = None
    ) -> None:
        """Tags a company_sync run with the list it syncs and, once done, its counts."""
        meta: dict[str, object] = {"list_source": list_source}
        if counts is not None:
            meta["counts"] = counts
        self._conn.execute(
            "UPDATE runs SET config_json = ? WHERE id = ?", (json.dumps(meta), run_id)
        )
        self._conn.commit()

    def active_company_sync(self, list_source: str, now: datetime) -> int | None:
        """The live sync of this list, if one is running. A row nobody has touched for the
        heartbeat timeout is a dead worker, not a sync in progress, so it does not count."""
        rows = self._conn.execute(
            "SELECT id, heartbeat_at, started_at, config_json FROM runs "
            "WHERE kind = 'company_sync' AND state = 'syncing' ORDER BY id DESC"
        ).fetchall()
        for row in rows:
            tagged = json.loads(row["config_json"] or "{}").get("list_source")
            if tagged == list_source and not stalled(row["heartbeat_at"] or row["started_at"], now):
                return int(row["id"])
        return None

    def start_stage(self, run_id: int, stage: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO run_stages (run_id, stage, started_at) VALUES (?,?,?)",
            (run_id, stage, now.isoformat()),
        )
        self._conn.commit()

    def end_stage(self, run_id: int, stage: str, now: datetime) -> None:
        """Closes the latest open row of this stage; does nothing if there is none."""
        self._conn.execute(
            "UPDATE run_stages SET ended_at = ? WHERE id = ("
            "SELECT MAX(id) FROM run_stages WHERE run_id = ? AND stage = ? AND ended_at IS NULL)",
            (now.isoformat(), run_id, stage),
        )
        self._conn.commit()

    def record_funnel(self, run_id: int, funnel: dict[str, Any]) -> None:
        self._conn.execute("UPDATE runs SET funnel = ? WHERE id = ?", (json.dumps(funnel), run_id))
        self._conn.commit()

    def finish_run(self, run_id: int, now: datetime, summary: str) -> None:
        self._conn.execute(
            "UPDATE runs SET ended_at = ?, summary = ?, state = 'succeeded' "
            "WHERE id = ? AND (state IN ('running','syncing') OR ended_at IS NULL)",
            (now.isoformat(), summary, run_id),
        )
        self._conn.commit()

    def record_source_run(
        self,
        run_id: int,
        source: str,
        identifier: str,
        ok: bool,
        fetched: int,
        error: str | None,
        now: datetime,
    ) -> None:
        """Every attempt is recorded so a silent failure never reads as zero jobs."""
        self._conn.execute(
            """
            INSERT INTO source_runs (run_id, source, identifier, ok, fetched, error, observed_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (run_id, source, identifier, int(ok), fetched, error, now.isoformat()),
        )
        self._conn.commit()

    # --- cooldowns --------------------------------------------------------

    def is_cooled_down(self, source: str, identifier: str, now: datetime) -> bool:
        """True when the target may be fetched again."""
        row = self._conn.execute(
            "SELECT next_scrape_at FROM cooldowns WHERE source = ? AND identifier = ?",
            (source, identifier),
        ).fetchone()
        return row is None or datetime.fromisoformat(row["next_scrape_at"]) <= now

    def last_scraped_by_target(self) -> dict[tuple[str, str], datetime]:
        """When each board was last visited, so a run can take the longest-waiting first.

        A board never visited has no row. A failed fetch also stamps the row, so a dead
        board rotates to the back instead of leading every run."""
        rows = self._conn.execute(
            "SELECT source, identifier, last_scraped_at FROM cooldowns"
        ).fetchall()
        return {
            (row["source"], row["identifier"]): datetime.fromisoformat(row["last_scraped_at"])
            for row in rows
        }

    def set_cooldown(
        self, source: str, identifier: str, now: datetime, hours: int, failed: bool = False
    ) -> None:
        """Exponential backoff on repeated failure; plain cooldown on success."""
        row = self._conn.execute(
            "SELECT failures FROM cooldowns WHERE source = ? AND identifier = ?",
            (source, identifier),
        ).fetchone()
        failures = (int(row["failures"]) if row else 0) + 1 if failed else 0
        wait = min(hours * (2**failures) if failures else hours, _MAX_BACKOFF_HOURS)
        self._conn.execute(
            """
            INSERT INTO cooldowns (source, identifier, last_scraped_at, next_scrape_at, failures)
            VALUES (?,?,?,?,?)
            ON CONFLICT (source, identifier) DO UPDATE SET
                last_scraped_at = excluded.last_scraped_at,
                next_scrape_at  = excluded.next_scrape_at,
                failures        = excluded.failures
            """,
            (
                source,
                identifier,
                now.isoformat(),
                (now + timedelta(hours=wait)).isoformat(),
                failures,
            ),
        )
        self._conn.commit()

    # --- source health ----------------------------------------------------

    def source_health(self, source: str) -> SourceHealth | None:
        row = self._conn.execute(
            "SELECT * FROM source_health WHERE source = ?", (source,)
        ).fetchone()
        if row is None:
            return None

        def when(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        return SourceHealth(
            source=row["source"],
            consecutive_failures=row["consecutive_failures"],
            paused_until=when(row["paused_until"]),
            pause_hours=row["pause_hours"],
            last_ok_at=when(row["last_ok_at"]),
            last_failure_at=when(row["last_failure_at"]),
            last_failure=FailureReason(row["last_failure"]) if row["last_failure"] else None,
            last_message=row["last_message"],
        )

    def save_source_health(self, health: SourceHealth) -> None:
        def text(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        self._conn.execute(
            """
            INSERT INTO source_health (source, consecutive_failures, paused_until, pause_hours,
                                       last_ok_at, last_failure_at, last_failure, last_message)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT (source) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                paused_until         = excluded.paused_until,
                pause_hours          = excluded.pause_hours,
                last_ok_at           = excluded.last_ok_at,
                last_failure_at      = excluded.last_failure_at,
                last_failure         = excluded.last_failure,
                last_message         = excluded.last_message
            """,
            (
                health.source,
                health.consecutive_failures,
                text(health.paused_until),
                health.pause_hours,
                text(health.last_ok_at),
                text(health.last_failure_at),
                health.last_failure.value if health.last_failure else None,
                health.last_message,
            ),
        )
        self._conn.commit()

    # --- source proposals -------------------------------------------------

    def insert_proposal(self, proposal: SourceProposal, now: datetime) -> int | None:
        """Store one pending proposal, or None when that board is already known.

        A duplicate is an expected outcome rather than an error: the agent is told what
        exists by `known_companies()`, and the unique index is what makes that advice
        binding — including for a board the user already rejected.
        """
        try:
            cursor = self._conn.execute(
                """
                INSERT INTO source_proposals
                    (board_type, slug, company, evidence, confidence, job_count,
                     sample_titles, status, run_id, proposed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    proposal.board_type,
                    proposal.slug,
                    proposal.company,
                    proposal.evidence,
                    proposal.confidence,
                    proposal.job_count,
                    "\n".join(proposal.sample_titles),
                    str(ProposalStatus.PENDING),
                    proposal.run_id,
                    now.isoformat(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        self._conn.commit()
        return int(cursor.lastrowid or 0)

    def list_proposals(
        self, status: ProposalStatus | None = ProposalStatus.PENDING, limit: int = 50
    ) -> Sequence[SourceProposal]:
        sql = "SELECT * FROM source_proposals"
        params: list[object] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(str(status))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [_to_proposal(row) for row in self._conn.execute(sql, params)]

    def get_proposal(self, proposal_id: int) -> SourceProposal | None:
        row = self._conn.execute(
            "SELECT * FROM source_proposals WHERE id = ?", (proposal_id,)
        ).fetchone()
        return _to_proposal(row) if row else None

    def reject_proposal(self, proposal_id: int, now: datetime) -> bool:
        """Reject without deleting. The row is what stops the agent re-proposing it."""
        cursor = self._conn.execute(
            "UPDATE source_proposals SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
            (str(ProposalStatus.REJECTED), now.isoformat(), proposal_id, "pending"),
        )
        self._conn.commit()
        return cursor.rowcount == 1

    def approve_proposal(self, proposal_id: int, now: datetime) -> SourceTarget | None:
        """Promote a pending proposal into the crawled target list, atomically.

        The status change and the insert are one transaction: a target that exists
        without an approved proposal behind it would be a board nobody agreed to crawl.
        """
        proposal = self.get_proposal(proposal_id)
        if proposal is None or proposal.status is not ProposalStatus.PENDING:
            return None
        with self._conn:
            self._conn.execute(
                "UPDATE source_proposals SET status = ?, decided_at = ? WHERE id = ?",
                (str(ProposalStatus.APPROVED), now.isoformat(), proposal_id),
            )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO source_targets
                    (source, identifier, tier, cooldown_hours, active, added_at,
                     from_proposal_id)
                VALUES (?,?,?,?,1,?,?)
                """,
                (
                    proposal.board_type,
                    proposal.slug,
                    0,
                    24,
                    now.isoformat(),
                    proposal_id,
                ),
            )
        return proposal.to_target()

    def list_source_targets(self, active_only: bool = True) -> Sequence[SourceTarget]:
        sql = "SELECT source, identifier, tier, cooldown_hours FROM source_targets"
        if active_only:
            sql += " WHERE active = 1"
        return [
            SourceTarget(
                source=row["source"],
                identifier=row["identifier"],
                tier=row["tier"],
                cooldown_hours=row["cooldown_hours"],
            )
            for row in self._conn.execute(sql + " ORDER BY id")
        ]

    def known_slugs(self) -> set[tuple[str, str]]:
        """Every board the store has an opinion about, whatever that opinion was."""
        known = {(target.source, target.identifier) for target in self.list_source_targets(False)}
        known |= {
            (row["board_type"], row["slug"])
            for row in self._conn.execute("SELECT board_type, slug FROM source_proposals")
        }
        return known

    # --- aliases ----------------------------------------------------------

    def company_aliases(self) -> dict[str, str]:
        rows = self._conn.execute("SELECT alias, canonical FROM company_aliases")
        return {row["alias"]: row["canonical"] for row in rows}

    def add_company_alias(self, alias: str, canonical: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO company_aliases (alias, canonical) VALUES (?,?)",
            (alias, canonical),
        )
        self._conn.commit()

    # --- company-list import -----------------------------------------------

    def upsert_import_company(self, list_source: str, ref: CompanyRef, now: datetime) -> None:
        """Record what the latest fetch saw. Resolution status is untouched here — a
        row already `resolved` stays that way until `record_resolution` says otherwise,
        and a row coming back from `inactive` starts fresh at `unresolved`."""
        self._conn.execute(
            """
            INSERT INTO import_companies
                (list_source, external_id, name, first_seen_at, last_seen_at, homepage_url)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT (list_source, external_id) DO UPDATE SET
                name = excluded.name,
                last_seen_at = excluded.last_seen_at,
                homepage_url = COALESCE(excluded.homepage_url, homepage_url),
                last_probed_at = CASE WHEN status = 'inactive' THEN NULL ELSE last_probed_at END,
                status = CASE WHEN status = 'inactive' THEN 'unresolved' ELSE status END
            """,
            (
                list_source,
                ref.external_id,
                ref.name,
                now.isoformat(),
                now.isoformat(),
                # A careers-board URL is where the jobs are, not the company's own site.
                None if extract_ats_hint(ref.url) else ref.url,
            ),
        )
        self._conn.commit()

    def get_import_company(self, list_source: str, external_id: str) -> ImportCompany | None:
        row = self._conn.execute(
            "SELECT * FROM import_companies WHERE list_source = ? AND external_id = ?",
            (list_source, external_id),
        ).fetchone()
        return _to_import_company(row) if row else None

    def get_import_company_by_id(self, company_id: int) -> ImportCompany | None:
        row = self._conn.execute(
            "SELECT * FROM import_companies WHERE id = ?", (company_id,)
        ).fetchone()
        return _to_import_company(row) if row else None

    def record_manual_resolution(
        self,
        list_source: str,
        external_id: str,
        board_type: str,
        slug: str,
        open_roles: int | None,
        now: datetime,
    ) -> None:
        """The user named this company's board. Resolved and pinned in one write, so a
        sync can never see the board without the pin."""
        self._conn.execute(
            """
            UPDATE import_companies
            SET status = ?, board_type = ?, slug = ?, last_error = NULL, last_seen_at = ?,
                last_probed_at = ?, open_roles = ?, pinned = 1
            WHERE list_source = ? AND external_id = ?
            """,
            (
                str(ImportStatus.RESOLVED),
                board_type,
                slug,
                now.isoformat(),
                now.isoformat(),
                open_roles,
                list_source,
                external_id,
            ),
        )
        self._conn.commit()

    def list_import_companies(
        self, list_source: str | None = None, statuses: Sequence[ImportStatus] | None = None
    ) -> Sequence[ImportCompany]:
        sql = "SELECT * FROM import_companies"
        clauses: list[str] = []
        params: list[object] = []
        if list_source is not None:
            clauses.append("list_source = ?")
            params.append(list_source)
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(str(status) for status in statuses)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        return [_to_import_company(row) for row in self._conn.execute(sql, params)]

    def record_resolution(
        self,
        list_source: str,
        external_id: str,
        status: ImportStatus,
        board_type: str | None,
        slug: str | None,
        last_error: str | None,
        now: datetime,
        open_roles: int | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE import_companies
            SET status = ?, board_type = ?, slug = ?, last_error = ?, last_seen_at = ?,
                last_probed_at = ?, open_roles = ?
            WHERE list_source = ? AND external_id = ?
            """,
            (
                str(status),
                board_type,
                slug,
                last_error,
                now.isoformat(),
                now.isoformat(),
                open_roles,
                list_source,
                external_id,
            ),
        )
        self._conn.commit()

    def insert_imported_target(self, target: SourceTarget, now: datetime, external_id: str) -> None:
        """Straight into the active crawl: importing a curated list is the approval.

        A board that is already crawled for another reason — seeded in `config.yaml`, or
        approved from an agent proposal — is reactivated but keeps its own `origin`. If
        the import took the row over, dropping the company from the watchlist would
        later deactivate a target the user never imported in the first place.
        """
        self._conn.execute(
            """
            INSERT INTO source_targets
                (source, identifier, tier, cooldown_hours, active, added_at,
                 origin, external_id)
            VALUES (?,?,?,?,1,?,'imported',?)
            ON CONFLICT (source, identifier) DO UPDATE SET
                active = 1,
                external_id = CASE WHEN origin = 'imported'
                    THEN excluded.external_id ELSE external_id END
            """,
            (
                target.source,
                target.identifier,
                target.tier,
                target.cooldown_hours,
                now.isoformat(),
                external_id,
            ),
        )
        self._conn.commit()

    def deactivate_missing_import_companies(
        self, list_source: str, seen_external_ids: Iterable[str], now: datetime
    ) -> tuple[str, ...]:
        """A company present in a previous sync but absent from this one goes `inactive`,
        never deleted — the same convention a job going stale already follows. Its
        imported target (if any) is deactivated the same way, not dropped."""
        seen = set(seen_external_ids)
        stale = [
            row["external_id"]
            for row in self._conn.execute(
                "SELECT external_id FROM import_companies "
                "WHERE list_source = ? AND status != 'inactive'",
                (list_source,),
            )
            if row["external_id"] not in seen
        ]
        with self._conn:
            for external_id in stale:
                self._conn.execute(
                    "UPDATE import_companies SET status = 'inactive', last_seen_at = ? "
                    "WHERE list_source = ? AND external_id = ?",
                    (now.isoformat(), list_source, external_id),
                )
                self._conn.execute(
                    "UPDATE source_targets SET active = 0 "
                    "WHERE origin = 'imported' AND external_id = ?",
                    (external_id,),
                )
        return tuple(stale)

    def imported_target_origins(self) -> dict[tuple[str, str], str]:
        """`origin` per `(source, identifier)`, so a CLI listing can label an imported
        target distinctly from an agent-approved or hand-configured one."""
        return {
            (row["source"], row["identifier"]): row["origin"]
            for row in self._conn.execute("SELECT source, identifier, origin FROM source_targets")
        }

    # --- sample roles -----------------------------------------------------

    def add_sample_role(self, role: SampleRole, now: datetime) -> tuple[SampleRole, bool]:
        """The stored row and whether it is new. The same posting twice is one row, so a
        double-click or a re-paste never weighs one example twice. Pasted text has no job
        id and is always new."""
        if role.source_job_id is not None:
            row = self._conn.execute(
                "SELECT * FROM sample_roles WHERE source = ? AND source_job_id = ? "
                "AND archived_at IS NULL",
                (role.source, role.source_job_id),
            ).fetchone()
            if row is not None:
                return _to_sample_role(row), False
        cur = self._conn.execute(
            "INSERT INTO sample_roles (url, source, slug, source_job_id, company, title, text, "
            "added_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                role.url,
                role.source,
                role.slug,
                role.source_job_id,
                role.company,
                role.title,
                role.text,
                now.isoformat(),
            ),
        )
        if not self._in_transaction:
            self._conn.commit()
        return replace(role, id=int(cur.lastrowid or 0), added_at=now), True

    def active_sample_roles(self) -> list[SampleRole]:
        """Newest first: the latest example is the one the user is thinking about."""
        rows = self._conn.execute(
            "SELECT * FROM sample_roles WHERE user_id IS NULL AND archived_at IS NULL "
            "ORDER BY id DESC"
        ).fetchall()
        return [_to_sample_role(row) for row in rows]

    def archive_sample_role(self, role_id: int, now: datetime) -> bool:
        """False when there is no such active role, so the API can answer 404."""
        cur = self._conn.execute(
            "UPDATE sample_roles SET archived_at = ? WHERE id = ? AND archived_at IS NULL",
            (now.isoformat(), role_id),
        )
        if not self._in_transaction:
            self._conn.commit()
        return cur.rowcount == 1

    def add_manual_target(
        self,
        source: str,
        slug: str,
        now: datetime,
        name: str | None = None,
        open_roles: int | None = None,
    ) -> None:
        """A board the user looked up by company name and confirmed. One already crawled
        for another reason is reactivated but keeps its own origin."""
        self._conn.execute(
            """
            INSERT INTO source_targets
                (source, identifier, tier, cooldown_hours, active, added_at, origin,
                 name, open_roles)
            VALUES (?,?,?,?,1,?,'manual',?,?)
            ON CONFLICT (source, identifier) DO UPDATE SET
                active = 1,
                name = COALESCE(name, excluded.name),
                open_roles = excluded.open_roles
            """,
            (source, slug, DEFAULT_TIER, DEFAULT_COOLDOWN_HOURS, now.isoformat(), name, open_roles),
        )
        if not self._in_transaction:
            self._conn.commit()

    def list_board_visits(self) -> Sequence[BoardVisit]:
        """Every active crawl target with the name it goes by, when it was added, and when
        it was last visited. Imported boards take their name and role count from the
        company list they came from."""
        rows = self._conn.execute(
            """
            SELECT t.source, t.identifier, t.tier, t.origin, t.added_at,
                   COALESCE(c.name, t.name) AS name,
                   COALESCE(c.status, 'resolved') AS status,
                   COALESCE(c.open_roles, t.open_roles) AS open_roles,
                   k.last_scraped_at
            FROM source_targets t
            LEFT JOIN import_companies c
                ON t.origin = 'imported' AND c.external_id = t.external_id
            LEFT JOIN cooldowns k
                ON k.source = t.source AND k.identifier = t.identifier
            WHERE t.active = 1
            ORDER BY t.id
            """
        ).fetchall()
        return [
            BoardVisit(
                name=row["name"] or row["identifier"],
                source=row["source"],
                slug=row["identifier"],
                origin=row["origin"],
                status=row["status"],
                tier=row["tier"],
                added_at=datetime.fromisoformat(row["added_at"]) if row["added_at"] else None,
                open_roles=row["open_roles"],
                last_visited_at=(
                    datetime.fromisoformat(row["last_scraped_at"])
                    if row["last_scraped_at"]
                    else None
                ),
            )
            for row in rows
        ]

    def add_sample_role_target(self, source: str, slug: str, now: datetime) -> None:
        """Crawl the sample role's company too. A board already crawled for another reason
        is reactivated but keeps its own origin, like an imported one."""
        self._conn.execute(
            """
            INSERT INTO source_targets
                (source, identifier, tier, cooldown_hours, active, added_at, origin)
            VALUES (?,?,?,?,1,?,'sample_role')
            ON CONFLICT (source, identifier) DO UPDATE SET active = 1
            """,
            (source, slug, DEFAULT_TIER, DEFAULT_COOLDOWN_HOURS, now.isoformat()),
        )
        if not self._in_transaction:
            self._conn.commit()

    def homepage_for_company(self, name: str) -> str | None:
        """The imported homepage of a company, matched on name ignoring case."""
        row = self._conn.execute(
            "SELECT homepage_url FROM import_companies "
            "WHERE homepage_url IS NOT NULL AND lower(name) = lower(?) "
            "ORDER BY last_seen_at DESC LIMIT 1",
            (name,),
        ).fetchone()
        return str(row[0]) if row else None

    def board_slug_for_company(self, name: str, board_type: str) -> str | None:
        """The slug a company-list import resolved for this vendor, if it did."""
        row = self._conn.execute(
            "SELECT slug FROM import_companies WHERE slug IS NOT NULL AND board_type = ? "
            "AND lower(name) = lower(?) ORDER BY last_seen_at DESC LIMIT 1",
            (board_type, name),
        ).fetchone()
        return str(row[0]) if row else None

    # --- feedback chat ----------------------------------------------------

    def has_active_run(self) -> bool:
        """Whether a search is running now, and so holding the local model."""
        row = self._conn.execute(
            "SELECT 1 FROM runs WHERE state IN ('running','syncing') AND ended_at IS NULL LIMIT 1"
        ).fetchone()
        return row is not None

    def add_feedback(self, role: str, text: str, actions: str | None, now: datetime) -> int:
        """Append one chat message. `actions` is the JSON list on an assistant row."""
        cur = self._conn.execute(
            "INSERT INTO feedback_entries (created_at, role, text, actions) VALUES (?,?,?,?)",
            (now.isoformat(), role, text, actions),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def recent_feedback(self, limit: int) -> list[FeedbackEntry]:
        """The last `limit` chat messages, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM feedback_entries ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [
            FeedbackEntry(
                int(row["id"]),
                row["role"],
                row["text"],
                row["actions"],
                datetime.fromisoformat(row["created_at"]),
            )
            for row in reversed(rows)
        ]

    def recent_corrections(self, limit: int, kinds: Sequence[str]) -> list[tuple[str, str, str]]:
        """The last `limit` corrections of the given kinds as (kind, target, note), oldest
        first. Batch notes are the old feedback box's rows, read now as earlier feedback."""
        marks = ",".join("?" for _ in kinds)
        rows = self._conn.execute(
            f"SELECT kind, target, note FROM corrections WHERE kind IN ({marks})"
            " ORDER BY id DESC LIMIT ?",
            (*kinds, limit),
        ).fetchall()
        return [(r["kind"], r["target"], r["note"]) for r in reversed(rows)]

    # --- recycle bin ------------------------------------------------------

    # Jobs whose latest decision is `dismissed` and which are not cleaned yet. The clock
    # reads the latest decision, so dismissing again after a restore starts a new period.
    _BIN = """
        SELECT j.id, j.title, j.company, d.created_at AS dismissed_at
        FROM jobs j
        JOIN job_decisions d ON d.id = (
            SELECT id FROM job_decisions WHERE job_id = j.id ORDER BY id DESC LIMIT 1
        )
        WHERE d.decision = 'dismissed' AND j.tombstoned_at IS NULL
    """

    def _bin_rows(self) -> list[sqlite3.Row]:
        return list(self._conn.execute(f"{self._BIN} ORDER BY d.created_at DESC, d.id DESC"))

    def in_bin(self, job_id: int) -> bool:
        return self._conn.execute(f"{self._BIN} AND j.id = ?", (job_id,)).fetchone() is not None

    def bin_items(self, now: datetime, days: int) -> list[BinItem]:
        """What the recycle bin shows, newest dismissal first. Expired jobs are cleaned
        first, so the list never offers one that has run out of time."""
        self.clean_expired(now, days)
        items = []
        for row in self._bin_rows():
            dismissed_at = datetime.fromisoformat(row["dismissed_at"])
            elapsed = (now - dismissed_at) // timedelta(days=1)
            items.append(
                BinItem(
                    job_id=int(row["id"]),
                    title=row["title"],
                    company=row["company"],
                    dismissed_at=dismissed_at,
                    days_left=max(0, days - elapsed),
                )
            )
        return items

    def bin_count(self, now: datetime, days: int) -> int:
        """How many jobs the bin holds, without cleaning. An expired job not cleaned yet is
        left out, so the count matches what the bin will show."""
        cutoff = now - timedelta(days=days)
        return sum(datetime.fromisoformat(row["dismissed_at"]) > cutoff for row in self._bin_rows())

    def restore_job(self, job_id: int, now: datetime) -> bool:
        """Take a job out of the bin. False if it is not there: cleaned, or not dismissed."""
        if not self.in_bin(job_id):
            return False
        self._write_decision(job_id, "restored", "", now)
        self._conn.commit()
        return True

    def clean_job(self, job_id: int, now: datetime) -> None:
        """Drop a job's heavy data for good. The row stays, so the crawler's dedupe still
        matches it and `upsert_job` leaves it alone."""
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return
        # Embeddings are keyed by the text the model read, so the key is rebuilt from the
        # row before its description is emptied.
        embedding_key = text_hash(job_text(self._to_stored(row).posting))
        with self.transaction():
            self._conn.execute("DELETE FROM scores WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM job_versions WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM job_outlines WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM job_embeddings WHERE text_hash = ?", (embedding_key,))
            self._conn.execute(
                "UPDATE jobs SET description = '', tombstoned_at = ? WHERE id = ?",
                (now.isoformat(), job_id),
            )

    def clean_expired(self, now: datetime, days: int) -> int:
        """Clean every bin job dismissed at least `days` ago. Returns how many."""
        cutoff = now - timedelta(days=days)
        expired = [
            int(row["id"])
            for row in self._bin_rows()
            if datetime.fromisoformat(row["dismissed_at"]) <= cutoff
        ]
        for job_id in expired:
            self.clean_job(job_id, now)
        return len(expired)

    # --- applications (Notion) ----------------------------------------------

    def notion_database(self) -> tuple[str, str] | None:
        """The saved Notion Applications database and its parent page, if one was found."""
        row = self._conn.execute(
            "SELECT database_id, page_id FROM notion_database WHERE id = 1"
        ).fetchone()
        return (str(row[0]), str(row[1])) if row else None

    def save_notion_database(self, database_id: str, page_id: str) -> None:
        self._conn.execute(
            "INSERT INTO notion_database (id, database_id, page_id) VALUES (1, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET database_id = excluded.database_id, "
            "page_id = excluded.page_id",
            (database_id, page_id),
        )
        self._conn.commit()

    def application_for_job(self, job_id: int) -> int | None:
        """The Notion Index of this job's row, or None when it was never added."""
        row = self._conn.execute(
            "SELECT id FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        return int(row[0]) if row else None

    def next_application_index(self) -> int:
        """The id the next `applications` row will get. Notion needs the Index before the
        local row can exist (the row stores the Notion page id), so it is read ahead."""
        row = self._conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'applications'"
        ).fetchone()
        return int(row[0]) + 1 if row else 1

    def record_application(self, index: int, job_id: int, page_id: str, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO applications (id, job_id, notion_page_id, created_at) VALUES (?,?,?,?)",
            (index, job_id, page_id, now.isoformat()),
        )
        self._conn.commit()


def _to_import_company(row: sqlite3.Row) -> ImportCompany:
    return ImportCompany(
        id=row["id"],
        list_source=row["list_source"],
        external_id=row["external_id"],
        name=row["name"],
        status=ImportStatus(row["status"]),
        board_type=row["board_type"],
        slug=row["slug"],
        last_error=row["last_error"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        last_probed_at=(
            datetime.fromisoformat(row["last_probed_at"]) if row["last_probed_at"] else None
        ),
        open_roles=row["open_roles"],
        pinned=bool(row["pinned"]),
    )


def _pack_vector(vec: Sequence[float]) -> bytes:
    """Little-endian float32, whatever the host's byte order, so a copied DB reads the same."""
    packed = array("f", vec)
    if sys.byteorder == "big":
        packed.byteswap()
    return packed.tobytes()


def _unpack_vector(blob: bytes) -> list[float]:
    vec = array("f")
    vec.frombytes(blob)
    if sys.byteorder == "big":
        vec.byteswap()
    return vec.tolist()


def _to_proposal(row: sqlite3.Row) -> SourceProposal:
    titles = row["sample_titles"] or ""
    return SourceProposal(
        id=row["id"],
        board_type=row["board_type"],
        slug=row["slug"],
        company=row["company"],
        evidence=row["evidence"],
        confidence=row["confidence"],
        job_count=row["job_count"],
        sample_titles=tuple(line for line in titles.split("\n") if line),
        status=ProposalStatus(row["status"]),
        run_id=row["run_id"],
        proposed_at=datetime.fromisoformat(row["proposed_at"]),
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
    )


def _to_sample_role(row: sqlite3.Row) -> SampleRole:
    return SampleRole(
        id=row["id"],
        source=row["source"],
        slug=row["slug"],
        source_job_id=row["source_job_id"],
        company=row["company"],
        title=row["title"],
        text=row["text"],
        url=row["url"],
        added_at=datetime.fromisoformat(row["added_at"]),
    )


def process_started_at(pid: int) -> str | None:
    """Linux process birth identity, including boot id to disambiguate PID reuse."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return f"{boot}:{stat[19]}"
    except (OSError, IndexError):
        return None
