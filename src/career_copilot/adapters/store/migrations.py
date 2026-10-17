"""Forward-only schema migrations, applied by version."""

from __future__ import annotations

import sqlite3

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE jobs (
            id                INTEGER PRIMARY KEY,
            source            TEXT NOT NULL,
            source_job_id     TEXT NOT NULL,
            company           TEXT NOT NULL,
            company_norm      TEXT NOT NULL,
            title             TEXT NOT NULL,
            title_norm        TEXT NOT NULL,
            location          TEXT NOT NULL,
            location_norm     TEXT NOT NULL,
            canonical_url     TEXT NOT NULL,
            description       TEXT NOT NULL,
            fingerprint       TEXT NOT NULL,
            content_hash      TEXT NOT NULL,
            posted_at         TEXT,
            employment_type   TEXT,
            remote            INTEGER,
            filter_outcome    TEXT NOT NULL,
            filter_reasons    TEXT NOT NULL DEFAULT '',
            filter_warnings   TEXT NOT NULL DEFAULT '',
            status            TEXT NOT NULL DEFAULT 'active',
            needs_rescore     INTEGER NOT NULL DEFAULT 1,
            first_seen_at     TEXT NOT NULL,
            last_seen_at      TEXT NOT NULL
        );

        -- Dedupe is enforced by the database, not by in-memory checks.
        CREATE UNIQUE INDEX idx_jobs_source_id ON jobs (source, source_job_id);
        CREATE UNIQUE INDEX idx_jobs_url ON jobs (canonical_url);
        CREATE UNIQUE INDEX idx_jobs_fingerprint ON jobs (fingerprint);
        CREATE INDEX idx_jobs_outcome ON jobs (filter_outcome, last_seen_at);

        CREATE TABLE job_versions (
            id            INTEGER PRIMARY KEY,
            job_id        INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
            content_hash  TEXT NOT NULL,
            observed_at   TEXT NOT NULL
        );
        CREATE INDEX idx_job_versions_job ON job_versions (job_id, observed_at);

        CREATE TABLE runs (
            id          INTEGER PRIMARY KEY,
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            summary     TEXT
        );

        CREATE TABLE source_runs (
            id          INTEGER PRIMARY KEY,
            run_id      INTEGER NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
            source      TEXT NOT NULL,
            identifier  TEXT NOT NULL,
            ok          INTEGER NOT NULL,
            fetched     INTEGER NOT NULL DEFAULT 0,
            error       TEXT,
            observed_at TEXT NOT NULL
        );

        CREATE TABLE cooldowns (
            source          TEXT NOT NULL,
            identifier      TEXT NOT NULL,
            last_scraped_at TEXT NOT NULL,
            next_scrape_at  TEXT NOT NULL,
            failures        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (source, identifier)
        );

        CREATE TABLE company_aliases (
            alias      TEXT PRIMARY KEY,
            canonical  TEXT NOT NULL
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE scores (
            id              INTEGER PRIMARY KEY,
            job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
            stage           TEXT    NOT NULL,
            score           INTEGER NOT NULL,
            verdict         TEXT    NOT NULL,
            reasons         TEXT    NOT NULL DEFAULT '',
            gaps            TEXT    NOT NULL DEFAULT '',
            warnings        TEXT    NOT NULL DEFAULT '',
            evidence_refs   TEXT    NOT NULL DEFAULT '',
            rubric_version  INTEGER NOT NULL,
            model           TEXT    NOT NULL,
            prompt_hash     TEXT    NOT NULL,
            content_hash    TEXT    NOT NULL,
            created_at      TEXT    NOT NULL
        );

        -- The database enforces "never rescore unchanged work", the same way it
        -- enforces dedupe. A rerun inserts nothing.
        CREATE UNIQUE INDEX idx_scores_identity
            ON scores (job_id, stage, rubric_version, model, content_hash);
        CREATE INDEX idx_scores_rank ON scores (stage, score DESC);

        ALTER TABLE runs ADD COLUMN llm_calls INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE runs ADD COLUMN cost_eur  REAL    NOT NULL DEFAULT 0.0;
        """,
    ),
    (
        3,
        """
        -- One row per saved version of the preferences. A JSON payload rather than a
        -- column per field: nothing queries inside preferences, and the schema then
        -- evolves without a migration per field.
        CREATE TABLE preferences (
            version     INTEGER PRIMARY KEY AUTOINCREMENT,
            payload     TEXT    NOT NULL,
            prompt_hash TEXT    NOT NULL,
            created_at  TEXT    NOT NULL
        );

        -- `version` answers "which preferences did I hold then"; `prompt_hash` answers
        -- "would this score come out the same today". The default '' never equals a real
        -- hash, so score rows written before this migration read as stale on the next run
        -- rather than being silently treated as current.
        ALTER TABLE scores ADD COLUMN preferences_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE scores ADD COLUMN preferences_hash    TEXT    NOT NULL DEFAULT '';

        DROP INDEX idx_scores_identity;
        CREATE UNIQUE INDEX idx_scores_identity
            ON scores (job_id, stage, rubric_version, model, content_hash, preferences_hash);
        """,
    ),
    (
        4,
        """
        ALTER TABLE scores ADD COLUMN cost_eur REAL;
        ALTER TABLE runs ADD COLUMN kind TEXT NOT NULL DEFAULT 'discover';
        ALTER TABLE runs ADD COLUMN state TEXT NOT NULL DEFAULT 'succeeded';
        ALTER TABLE runs ADD COLUMN stage TEXT NOT NULL DEFAULT '';
        ALTER TABLE runs ADD COLUMN completed INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE runs ADD COLUMN total INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE runs ADD COLUMN failed INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE runs ADD COLUMN budget_eur REAL NOT NULL DEFAULT 0.0;
        ALTER TABLE runs ADD COLUMN mode TEXT NOT NULL DEFAULT '';
        ALTER TABLE runs ADD COLUMN preferences_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE runs ADD COLUMN preferences_hash TEXT NOT NULL DEFAULT '';
        ALTER TABLE runs ADD COLUMN triage_model TEXT NOT NULL DEFAULT '';
        ALTER TABLE runs ADD COLUMN match_model TEXT NOT NULL DEFAULT '';
        ALTER TABLE runs ADD COLUMN rubric_version INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE runs ADD COLUMN failure_category TEXT;
        ALTER TABLE runs ADD COLUMN failure_message TEXT;
        ALTER TABLE runs ADD COLUMN owner_pid INTEGER;
        ALTER TABLE runs ADD COLUMN owner_started_at TEXT;
        ALTER TABLE runs ADD COLUMN heartbeat_at TEXT;
        ALTER TABLE runs ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}';
        CREATE UNIQUE INDEX idx_runs_single_active ON runs (state) WHERE state = 'running';
        """,
    ),
    (
        5,
        """
        -- What the user said about a posting. Append-only: no UPDATE, no DELETE, no
        -- unique index. Changing a label writes a second row, and the latest row per job
        -- is the current label, so the sequence of opinions survives as well as the
        -- opinion. Nothing reads these back into scoring yet; feedback-driven re-ranking
        -- is its own spec (BACKLOG.md, issue #8).
        CREATE TABLE reviews (
            id           INTEGER PRIMARY KEY,
            job_id       INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
            label        TEXT    NOT NULL,   -- good_fit | maybe | not_relevant
            note         TEXT    NOT NULL DEFAULT '',
            -- The posting the user was actually looking at. A label survives an edit to
            -- the advert; whether it still applies is a question this column makes
            -- answerable rather than one it decides.
            content_hash TEXT    NOT NULL,
            created_at   TEXT    NOT NULL
        );
        CREATE INDEX idx_reviews_job ON reviews (job_id, id);

        -- Where the user says a rule or a quoted claim is wrong. `job_id` is nullable:
        -- a batch note is about the run, not about one posting.
        CREATE TABLE corrections (
            id          INTEGER PRIMARY KEY,
            job_id      INTEGER REFERENCES jobs (id) ON DELETE CASCADE,
            kind        TEXT    NOT NULL,   -- evidence | rule | batch
            target      TEXT    NOT NULL DEFAULT '',
            note        TEXT    NOT NULL DEFAULT '',
            created_at  TEXT    NOT NULL
        );
        CREATE INDEX idx_corrections_job ON corrections (job_id, id);
        """,
    ),
    (
        6,
        """
        -- What the discovery agent found. A proposal is a claim with evidence, never a
        -- change: nothing here affects a crawl until a row is approved and copied into
        -- source_targets. The unique index is the real dedupe — `known_companies()`
        -- tells the model what exists, the database enforces it.
        CREATE TABLE source_proposals (
            id            INTEGER PRIMARY KEY,
            board_type    TEXT    NOT NULL,   -- a key of SOURCE_TYPES
            slug          TEXT    NOT NULL,
            company       TEXT    NOT NULL,
            evidence      TEXT    NOT NULL,
            confidence    REAL    NOT NULL DEFAULT 0,
            job_count     INTEGER NOT NULL DEFAULT 0,
            sample_titles TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
            run_id        INTEGER REFERENCES runs (id) ON DELETE SET NULL,
            proposed_at   TEXT    NOT NULL,
            decided_at    TEXT
        );
        CREATE UNIQUE INDEX idx_source_proposals_board ON source_proposals (board_type, slug);
        CREATE INDEX idx_source_proposals_status ON source_proposals (status, id);

        -- Boards the user approved. config.yaml `targets:` stays the hand-written seed and
        -- is still read; this table is what a run adds to it, so a machine never edits the
        -- user's YAML.
        CREATE TABLE source_targets (
            id               INTEGER PRIMARY KEY,
            source           TEXT    NOT NULL,
            identifier       TEXT    NOT NULL,
            tier             INTEGER NOT NULL DEFAULT 0,
            cooldown_hours   INTEGER NOT NULL DEFAULT 24,
            active           INTEGER NOT NULL DEFAULT 1,
            added_at         TEXT    NOT NULL,
            from_proposal_id INTEGER REFERENCES source_proposals (id) ON DELETE SET NULL
        );
        CREATE UNIQUE INDEX idx_source_targets_identity ON source_targets (source, identifier);
        """,
    ),
    (
        7,
        """
        -- A curated watchlist (Notion, CSV) is higher-trust input than a discovery
        -- proposal: the user already did the "worth watching" judgment, so a resolved
        -- row goes straight into source_targets rather than through pending/approved.
        -- The unique index is the natural key a re-sync upserts against, exactly the
        -- way source_proposals' index is the real dedupe for the agent.
        CREATE TABLE import_companies (
            id             INTEGER PRIMARY KEY,
            list_source    TEXT    NOT NULL,   -- 'notion' | 'csv'
            external_id    TEXT    NOT NULL,   -- Notion page id / hash of normalized name
            name           TEXT    NOT NULL,
            status         TEXT    NOT NULL DEFAULT 'unresolved',
                -- unresolved | resolved | blocked | inactive
            board_type     TEXT,
            slug           TEXT,
            last_error     TEXT,   -- set when status = 'blocked': the probe's failure
            first_seen_at  TEXT    NOT NULL,
            last_seen_at   TEXT    NOT NULL
        );
        CREATE UNIQUE INDEX idx_import_companies_identity
            ON import_companies (list_source, external_id);
        CREATE INDEX idx_import_companies_status ON import_companies (status, id);

        -- origin distinguishes an imported row from an agent-approved one, the same way
        -- from_proposal_id already does for the agent; external_id is the join key back
        -- to import_companies for reconciliation on sync.
        ALTER TABLE source_targets ADD COLUMN origin TEXT NOT NULL DEFAULT 'approved';
        ALTER TABLE source_targets ADD COLUMN external_id TEXT;
        """,
    ),
    (
        8,
        """
        -- When a company was last probed and how many jobs its board held. A row probed
        -- recently is not probed again, so a re-sync does not re-ask a hundred hosts for
        -- an answer that has not changed.
        ALTER TABLE import_companies ADD COLUMN last_probed_at TEXT;
        ALTER TABLE import_companies ADD COLUMN open_roles INTEGER;
        """,
    ),
    (
        9,
        """
        -- The CV, uploaded once and versioned by the hash of its normalized text. Rows are
        -- immutable; the newest upload is the current one. `user_id` is a seam for a later
        -- accounts layer and is always NULL today.
        CREATE TABLE cv_versions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER,
            version      INTEGER NOT NULL,
            sha256       TEXT    NOT NULL,
            filename     TEXT    NOT NULL,
            text         TEXT    NOT NULL,
            uploaded_at  TEXT    NOT NULL,
            is_current   INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX idx_cv_sha ON cv_versions (COALESCE(user_id, 0), sha256);
        CREATE UNIQUE INDEX idx_cv_version ON cv_versions (COALESCE(user_id, 0), version);
        CREATE UNIQUE INDEX idx_cv_current ON cv_versions (COALESCE(user_id, 0))
            WHERE is_current = 1;

        -- Provenance only: which CV produced a score. Deliberately NOT in
        -- idx_scores_identity, so a new CV never makes an existing score stale. NULL means
        -- the score predates versioned CVs.
        ALTER TABLE scores ADD COLUMN cv_sha256 TEXT;
        """,
    ),
    (
        10,
        """
        -- What the user decided about a posting. Append-only: the current decision is the
        -- latest row for the job, so a later "applied" / Notion hand-off can replay the log.
        -- Attached to the job, not to a CV or a content hash, so a dismissed posting stays
        -- dismissed when its advert is edited. `user_id` is a seam, always NULL today.
        CREATE TABLE job_decisions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            job_id      INTEGER NOT NULL REFERENCES jobs(id),
            decision    TEXT    NOT NULL,   -- maybe | dismissed | saved
            note        TEXT,
            created_at  TEXT    NOT NULL
        );
        CREATE INDEX idx_decisions_job ON job_decisions (job_id, id DESC);

        -- The first time the user was shown a posting. No row = new to the user.
        CREATE TABLE job_views (
            job_id     INTEGER PRIMARY KEY REFERENCES jobs(id),
            user_id    INTEGER,
            viewed_at  TEXT NOT NULL
        );
        """,
    ),
    (
        11,
        """
        -- A board the user pointed a company at by hand. A pinned row is re-checked on
        -- every sync but never re-resolved, so a later `--refresh` cannot undo the answer.
        ALTER TABLE import_companies ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        12,
        """
        -- What the user is looking for, in their own words. Append-only: an entry is never
        -- edited, and removing one archives it, so the history the agent learned from
        -- stays readable. `user_id` is a seam, always NULL today.
        CREATE TABLE intent_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            text        TEXT    NOT NULL,
            kind        TEXT    NOT NULL DEFAULT 'entry',   -- entry | condensed
            created_at  TEXT    NOT NULL,
            archived_at TEXT
        );
        CREATE INDEX idx_intent_active ON intent_entries (archived_at, id);

        -- The old single field becomes the first entry, so nothing the user wrote is lost.
        INSERT INTO intent_entries (text, kind, created_at)
        SELECT trim(json_extract(payload, '$.role_intent')), 'entry', created_at
        FROM preferences
        WHERE version = (SELECT MAX(version) FROM preferences)
          AND trim(COALESCE(json_extract(payload, '$.role_intent'), '')) <> '';
        """,
    ),
    (
        13,
        """
        -- Postings the user holds up as "roles like this". Kept as text so the agent reads
        -- them even after the board drops the job. Removing one archives it.
        CREATE TABLE sample_roles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER,
            url           TEXT,
            source        TEXT NOT NULL,            -- a SOURCE_TYPES key, or 'manual'
            slug          TEXT,
            source_job_id TEXT,
            company       TEXT NOT NULL,
            title         TEXT NOT NULL,
            text          TEXT NOT NULL,
            added_at      TEXT NOT NULL,
            archived_at   TEXT
        );
        CREATE UNIQUE INDEX idx_sample_roles_job ON sample_roles (source, source_job_id)
            WHERE archived_at IS NULL AND source_job_id IS NOT NULL;
        """,
    ),
    (
        14,
        """
        -- A posting's embedding, keyed by the exact text the model read and which model
        -- read it, so an unchanged posting is never embedded twice and a model swap
        -- re-embeds cleanly. `text_hash` is sha256 of that text (title, company, location,
        -- description head), not `jobs.content_hash`, which covers the description alone.
        -- `vector` is little-endian float32 (`array('f')`).
        CREATE TABLE job_embeddings (
            text_hash  TEXT NOT NULL,
            model      TEXT NOT NULL,
            dim        INTEGER NOT NULL,
            vector     BLOB NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (text_hash, model)
        );
        """,
    ),
    (
        15,
        """
        -- What the profiler agent understood from the intent log, sample roles and CV.
        -- One row per distinct input, so an unchanged log reuses its profile for free.
        CREATE TABLE search_profiles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            inputs_hash TEXT NOT NULL UNIQUE,
            payload     TEXT NOT NULL,
            model       TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        """,
    ),
    (
        16,
        """
        -- Observability: one row per model request or agent run, one per agent tool call.
        -- Numbers, names and ids only; prompts, replies and tool arguments are never stored.
        CREATE TABLE llm_calls (
            id         INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            run_id     INTEGER,
            stage      TEXT NOT NULL,
            model      TEXT NOT NULL,
            backend    TEXT NOT NULL,
            profile    TEXT,
            kind       TEXT NOT NULL,
            requests   INTEGER NOT NULL DEFAULT 1,
            tokens_in  INTEGER,
            tokens_out INTEGER,
            latency_s  REAL NOT NULL,
            outcome    TEXT NOT NULL,
            retry      INTEGER NOT NULL DEFAULT 0,
            agent_run  TEXT
        );
        CREATE INDEX idx_llm_calls_created ON llm_calls (created_at);
        CREATE TABLE tool_calls (
            id         INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            run_id     INTEGER,
            agent      TEXT NOT NULL,
            tool       TEXT NOT NULL,
            latency_s  REAL NOT NULL,
            outcome    TEXT NOT NULL,
            agent_run  TEXT
        );
        CREATE INDEX idx_tool_calls_agent_run ON tool_calls (agent_run);
        CREATE INDEX idx_tool_calls_created ON tool_calls (created_at);
        CREATE TABLE llm_daily (
            day        TEXT NOT NULL,
            model      TEXT NOT NULL,
            backend    TEXT NOT NULL,
            stage      TEXT NOT NULL,
            calls      INTEGER NOT NULL,
            tokens_in  INTEGER NOT NULL,
            tokens_out INTEGER NOT NULL,
            latency_s  REAL NOT NULL,
            errors     INTEGER NOT NULL,
            PRIMARY KEY (day, model, backend, stage)
        );
        CREATE TABLE tool_daily (
            day       TEXT NOT NULL,
            agent     TEXT NOT NULL,
            tool      TEXT NOT NULL,
            calls     INTEGER NOT NULL,
            latency_s REAL NOT NULL,
            errors    INTEGER NOT NULL,
            PRIMARY KEY (day, agent, tool)
        );
        CREATE TABLE app_sessions (
            id         INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            ended_at   TEXT,
            outcome    TEXT,
            profile    TEXT,
            log_path   TEXT NOT NULL,
            log_gz     BLOB
        );
        """,
    ),
    (
        17,
        """
        -- The company's own website, when the imported list had one. Kept apart from
        -- `slug` because a careers-board URL says where the jobs are, not who the company is.
        ALTER TABLE import_companies ADD COLUMN homepage_url TEXT;
        """,
    ),
    (
        18,
        """
        -- The chat under Recommendations: every user message and every reply, so a failed
        -- model call never loses what the user wrote.
        CREATE TABLE feedback_entries (
            id         INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            role       TEXT NOT NULL,
            text       TEXT NOT NULL,
            actions    TEXT
        );
        """,
    ),
    (
        19,
        """
        -- A cleaned job: its bin time ran out or the user deleted it for good. The row
        -- stays so the crawler's dedupe still matches it; NULL means a full row.
        ALTER TABLE jobs ADD COLUMN tombstoned_at TEXT;
        """,
    ),
    (
        20,
        """
        -- The Notion "Applications" database the app found or created; one row at most.
        CREATE TABLE notion_database (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            database_id TEXT NOT NULL,
            page_id     TEXT NOT NULL
        );
        -- One row per job added to Notion. `id` is the Notion "Index", so it starts at 1
        -- and never repeats (AUTOINCREMENT never reuses a deleted id).
        CREATE TABLE applications (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id         INTEGER NOT NULL UNIQUE,
            notion_page_id TEXT NOT NULL,
            created_at     TEXT NOT NULL
        );
        """,
    ),
    (
        21,
        """
        -- Score breakdown: the four named parts behind a match score, as JSON
        -- {"skills": {"points": 30, "reason": "..."}, ...}. NULL for scores written before
        -- breakdowns existed (rubric 1) and for triage scores.
        ALTER TABLE scores ADD COLUMN parts TEXT;
        """,
    ),
    (
        22,
        """
        -- Boards were seeded with a 72h cooldown; the default is now 48h.
        UPDATE source_targets SET cooldown_hours = 48 WHERE cooldown_hours = 72;
        """,
    ),
    (
        23,
        """
        -- How long each stage of a run took. `ended_at` stays NULL while the stage runs, or
        -- when the run died inside it.
        CREATE TABLE run_stages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id     INTEGER NOT NULL REFERENCES runs (id),
            stage      TEXT    NOT NULL,
            started_at TEXT    NOT NULL,
            ended_at   TEXT
        );
        CREATE INDEX idx_run_stages_run ON run_stages (run_id, id);
        -- What discovery and scoring did, as JSON (spec 013). NULL for older runs.
        ALTER TABLE runs ADD COLUMN funnel TEXT;
        """,
    ),
    (
        24,
        """
        -- Which model serves each role, as chosen on the Settings page (spec 014). One row
        -- per save, like `preferences`: the newest is in force. Never holds a key.
        CREATE TABLE model_settings (
            version       INTEGER PRIMARY KEY AUTOINCREMENT,
            preset        TEXT,
            overrides     TEXT    NOT NULL,
            cloud_consent INTEGER NOT NULL DEFAULT 0,
            saved_at      TEXT    NOT NULL
        );
        """,
    ),
    (
        25,
        """
        -- Small per-account yes/no choices the UI remembers, such as whether to show the
        -- cloud-run privacy notice again. One row per flag; the value is a boolean.
        CREATE TABLE ui_flags (
            name  TEXT    PRIMARY KEY,
            value INTEGER NOT NULL
        );
        """,
    ),
    (
        26,
        """
        -- What each model call cost, as the provider billed it. Calls recorded before this
        -- read 0: the amount was never kept, so it is unknown rather than free.
        ALTER TABLE llm_calls ADD COLUMN cost_eur REAL NOT NULL DEFAULT 0.0;
        ALTER TABLE llm_daily ADD COLUMN cost_eur REAL NOT NULL DEFAULT 0.0;
        """,
    ),
    (
        27,
        """
        -- A reposted job that was hidden because the job it repeats was dismissed. NULL for
        -- every other job. The dismissal itself is a normal row in job_decisions.
        ALTER TABLE jobs ADD COLUMN repost_of INTEGER REFERENCES jobs (id);
        """,
    ),
    (
        28,
        """
        -- Why a job was dismissed, picked by the user after the click. NULL when no reason
        -- was given. Read only to build the note the match step is shown.
        ALTER TABLE job_decisions ADD COLUMN reason TEXT;
        """,
    ),
    (
        29,
        """
        -- Prompt tokens the provider served from its prefix cache. Calls recorded before this
        -- read 0: the count was never kept, so it is unknown rather than a miss.
        ALTER TABLE llm_calls ADD COLUMN cached_tokens INTEGER NOT NULL DEFAULT 0;
        """,
    ),
    (
        30,
        """
        -- The responsibilities and requirements pulled out of a posting for its card, as JSON
        -- lists of sentences copied from the text. Valid only while content_hash matches the
        -- job's, so a changed posting is read again. Display only: nothing scores from it.
        CREATE TABLE job_outlines (
            job_id           INTEGER PRIMARY KEY REFERENCES jobs (id),
            content_hash     TEXT NOT NULL,
            responsibilities TEXT NOT NULL,
            requirements     TEXT NOT NULL,
            model            TEXT NOT NULL,
            created_at       TEXT NOT NULL
        );
        """,
    ),
    (
        31,
        """
        -- What the person typed when they added a board, and how many roles it held then.
        -- NULL for every other origin, and for boards added before this.
        ALTER TABLE source_targets ADD COLUMN name TEXT;
        ALTER TABLE source_targets ADD COLUMN open_roles INTEGER;
        """,
    ),
)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    current: int = row[0]
    for version, sql in MIGRATIONS:
        if version > current:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
            current = version
    conn.commit()
    return current
