"""The ports. A port exists only where a second implementation is planned."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from career_copilot.domain.cv import SavedCv, StoredCv
from career_copilot.domain.intent import IntentEntry, IntentKind
from career_copilot.domain.models import (
    DigestStats,
    FilterOutcome,
    HardFilterResult,
    JobPosting,
    RawJob,
    SourceTarget,
    StoredJob,
    UpsertOutcome,
)
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import Progress
from career_copilot.domain.sample_roles import SampleRole
from career_copilot.domain.scoring import Score, ScoredJob
from career_copilot.domain.search_profile import SearchProfile
from career_copilot.domain.source_health import SourceHealth
from career_copilot.domain.sources import (
    BoardVisit,
    CompanyRef,
    ImportCompany,
    ImportStatus,
    SourceProposal,
)


@runtime_checkable
class JobSource(Protocol):
    """Fetches raw postings for one target. Implementations must not raise on empty."""

    name: str

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]: ...


class StateStore(Protocol):
    """Canonical state: jobs, observation history, run ledger, cooldowns."""

    def upsert_job(
        self, posting: JobPosting, verdict: HardFilterResult, now: datetime
    ) -> tuple[int, UpsertOutcome]: ...

    def list_jobs(
        self, outcome: FilterOutcome | None = None, limit: int = 50
    ) -> Sequence[StoredJob]: ...

    def update_run_progress(self, run_id: int, progress: Progress, now: datetime) -> None: ...

    def start_run(self, now: datetime, kind: str = "discover") -> int: ...

    def finish_run(self, run_id: int, now: datetime, summary: str) -> None: ...

    def record_source_run(
        self,
        run_id: int,
        source: str,
        identifier: str,
        ok: bool,
        fetched: int,
        error: str | None,
        now: datetime,
        reason: str | None = None,
        duration_ms: int | None = None,
    ) -> None: ...

    def is_cooled_down(self, source: str, identifier: str, now: datetime) -> bool: ...

    def set_cooldown(
        self, source: str, identifier: str, now: datetime, hours: int, failed: bool = False
    ) -> None: ...

    def last_scraped_by_target(self) -> dict[tuple[str, str], datetime]: ...

    def source_health(self, source: str) -> SourceHealth | None: ...

    def save_source_health(self, health: SourceHealth) -> None: ...

    def list_board_visits(self) -> Sequence[BoardVisit]: ...

    def company_aliases(self) -> dict[str, str]: ...

    def has_jobs(self) -> bool: ...

    def has_match_scores(self) -> bool: ...

    def all_active_jobs(self) -> Sequence[StoredJob]: ...

    def update_filter_result(self, job_id: int, verdict: HardFilterResult) -> None: ...

    def jobs_needing_score(
        self,
        model: str,
        rubric_version: int,
        limit: int | None = None,
        preferences_hash: str = "",
    ) -> Sequence[StoredJob]: ...

    def record_score(self, score: Score, now: datetime, cost_eur: float | None = None) -> int: ...

    def match_scores(
        self,
        limit: int | None = 50,
        model: str | None = None,
        rubric_version: int | None = None,
        max_per_company: int | None = None,
        preferences_hash: str | None = None,
    ) -> Sequence[ScoredJob]: ...

    def triage_scores(self, limit: int | None = None) -> Sequence[ScoredJob]: ...

    def digest_stats(
        self,
        model: str | None = None,
        rubric_version: int | None = None,
        preferences_hash: str | None = None,
    ) -> DigestStats: ...

    def clear_needs_rescore(self, job_id: int) -> None: ...

    def record_run_cost(self, run_id: int, calls: int, cost_eur: float) -> None: ...


class PreferencesStore(Protocol):
    """Durable, versioned preferences. Separate from `StateStore`: jobs and scores are a
    crawl's output, preferences are the user's input, and only the latter is edited by
    hand from a screen."""

    def load_current(self) -> Preferences | None: ...

    def load_version(self, version: int) -> Preferences | None: ...

    def save(self, prefs: Preferences, now: datetime) -> Preferences: ...


class CvStore(Protocol):
    """Durable, versioned CVs. The newest upload is current; older versions are history."""

    def save_cv(self, filename: str, text: str, now: datetime) -> SavedCv: ...

    def current_cv(self) -> StoredCv | None: ...

    def list_cvs(self) -> Sequence[StoredCv]: ...

    def delete_cvs(self) -> int: ...


class IntentStore(Protocol):
    """The user's own words about what they want, append-only."""

    def add_intent(
        self, text: str, now: datetime, kind: IntentKind = IntentKind.ENTRY
    ) -> IntentEntry: ...

    def active_intent(self) -> list[IntentEntry]: ...

    def archive_intent(self, entry_id: int, now: datetime) -> bool: ...


class ResumeMissingError(Exception):
    """A run needs a CV and none is stored (and no legacy file could seed one)."""


ResponseValidator = Callable[[dict[str, object]], None]

# Resume file bytes to plain text. A type alias rather than a Protocol: one
# method, and the planned LLM extraction pass is a plain function too.
ResumeTextExtractor = Callable[[str, bytes], str]


class FailureCategory(StrEnum):
    TRANSPORT = "transport"
    CREDIT = "credit"
    RATE_LIMIT = "rate_limit"
    PROVIDER = "provider"
    SCHEMA = "schema"
    EVIDENCE = "evidence"


FAILURE_MESSAGES = {
    FailureCategory.TRANSPORT: "The provider could not be reached. Try again later.",
    FailureCategory.CREDIT: "Provider credit is exhausted. Check your balance and spending limit.",
    FailureCategory.RATE_LIMIT: "The provider rate limit was reached. Wait before trying again.",
    FailureCategory.PROVIDER: (
        "The provider returned no usable answer. Try another model or retry later."
    ),
    FailureCategory.SCHEMA: "The model returned invalid output. Try another model.",
    FailureCategory.EVIDENCE: "The model made an unsupported claim. Review the source evidence.",
}


class LLMCallError(RuntimeError):
    """A failed model call, including the billable work known to the adapter."""

    def __init__(
        self,
        message: str,
        *,
        calls: int = 0,
        cost_eur: float = 0.0,
        category: FailureCategory = FailureCategory.TRANSPORT,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.calls = calls
        self.cost_eur = cost_eur


class SchemaError(LLMCallError):
    """The model could not produce output matching the schema.

    Lives with the port, not with an adapter: use cases catch it, and a use case
    importing an adapter would invert the dependency.
    """

    def __init__(
        self,
        message: str,
        *,
        calls: int = 0,
        cost_eur: float = 0.0,
        category: FailureCategory = FailureCategory.SCHEMA,
    ) -> None:
        super().__init__(message, calls=calls, cost_eur=cost_eur, category=category)


PII_CATEGORIES: tuple[str, ...] = (
    "PERSON",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "URL",
    "IBAN",
    "CREDIT_CARD",
    "IP",
)


@dataclass(frozen=True, slots=True)
class Detection:
    """One PII span found by a detector. Offsets are into the string exactly as given."""

    start: int
    length: int
    category: str  # PERSON | EMAIL | PHONE | ADDRESS | URL | IBAN | CREDIT_CARD | IP

    @property
    def end(self) -> int:
        return self.start + self.length


@runtime_checkable
class PiiDetector(Protocol):
    """Finds PII spans. Offsets are into each string exactly as given."""

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]: ...


class PiiRedactionError(LLMCallError):
    """Redaction could not be completed. The provider was never called."""


class PiiModelMissingError(PiiRedactionError):
    """The local detection model is not installed on this machine.

    Separate from the generic failure because it is the one redaction failure with a known,
    one-command remedy, and naming it lets the UI and `doctor` say which model to install
    instead of reporting an opaque failure class. `model_name` is a configuration constant,
    never analysed text, so disclosing it does not breach §11.
    """

    def __init__(self, model_name: str) -> None:
        super().__init__(f"spaCy model {model_name!r} is not installed")
        self.model_name = model_name


def _validate_value(value: object, schema: dict[str, object], path: str) -> None:
    expected = schema.get("type")
    valid_type = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
    }.get(str(expected), True)
    if not valid_type:
        raise SchemaError(f"{path} must be {expected}")
    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        raise SchemaError(f"{path} must be one of {enum}")
    if isinstance(value, int) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, int | float) and value < minimum:
            raise SchemaError(f"{path} must be >= {minimum}")
        if isinstance(maximum, int | float) and value > maximum:
            raise SchemaError(f"{path} must be <= {maximum}")
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            raise SchemaError(f"{path} must contain at least {min_items} item(s)")
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _validate_value(item, items, f"{path}[{index}]")


def validate_schema(data: object, schema: dict[str, object]) -> dict[str, object]:
    """Validate the JSON Schema subset used by all model adapters."""
    _validate_value(data, schema, "response")
    if not isinstance(data, dict):
        raise SchemaError("response must be object")
    required = schema.get("required", [])
    if isinstance(required, list):
        missing = [key for key in required if key not in data]
        if missing:
            raise SchemaError(f"missing required field(s): {', '.join(str(m) for m in missing)}")
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, value in data.items():
            field_schema = properties.get(key)
            if isinstance(field_schema, dict):
                _validate_value(value, field_schema, str(key))
    return data


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One model reply: validated data, the model that produced it, and its cost."""

    data: dict[str, object]
    model: str
    cost_eur: float = 0.0
    calls: int = 1
    tokens_in: int | None = None
    tokens_out: int | None = None
    latency_s: float | None = None
    cached_tokens: int = 0  # prompt tokens the provider served from its prefix cache


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    """One model request or agent run. Numbers, names and ids only, never text."""

    stage: str
    model: str
    backend: str
    kind: str  # request | agent_run
    latency_s: float
    outcome: str  # ok, or a FailureCategory value
    profile: str | None = None
    run_id: int | None = None
    requests: int = 1
    tokens_in: int | None = None
    tokens_out: int | None = None
    retry: int = 0
    agent_run: str | None = None  # groups an agent run with its tool calls
    cost_eur: float = 0.0  # what the provider billed; 0 for a local model
    cached_tokens: int = 0  # prompt tokens served from the provider's prefix cache


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One agent tool call. The arguments are never stored."""

    agent: str
    tool: str
    latency_s: float
    outcome: str  # ok | refused | error
    run_id: int | None = None
    agent_run: str | None = None


@runtime_checkable
class Recorder(Protocol):
    """Where call records go. An implementation must never raise into a run."""

    def llm_call(self, call: LlmCallRecord) -> None: ...

    def tool_call(self, call: ToolCallRecord) -> None: ...


@runtime_checkable
class LLMClient(Protocol):
    """Schema-constrained model access. Agents are built on top of this."""

    def complete(
        self,
        prompt: str,
        schema: dict[str, object],
        system: str | None = None,
        validator: ResponseValidator | None = None,
    ) -> LLMResponse: ...


class Embedder(Protocol):
    """Text to fixed-length vectors. Local and deterministic: no provider, no PII boundary.

    `model` names the vectors' space, so a cache keyed by it never mixes two models.
    """

    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class JobVectorStore(Protocol):
    """Embeddings cached by posting text and model, so only new text costs embedding time."""

    def job_vectors(self, model: str, text_hashes: Iterable[str]) -> dict[str, list[float]]: ...

    def save_job_vectors(
        self, model: str, items: Iterable[tuple[str, Sequence[float]]], now: datetime
    ) -> None: ...


class RepostStore(JobVectorStore, Protocol):
    """What the repost check reads and writes, beyond cached vectors."""

    def get_job(self, job_id: int) -> StoredJob | None: ...

    def dismissed_siblings(self, job_id: int) -> Sequence[StoredJob]: ...

    def is_cleaned(self, job_id: int) -> bool: ...

    def hide_as_repost(self, job_id: int, of_job_id: int, now: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One web search hit. The agent reads these; nothing else does."""

    title: str
    url: str
    snippet: str = ""


class SourceCatalog(Protocol):
    """The slice of the store a discovery run touches: what is known, and what was found.

    A narrow port rather than more methods on `StateStore`, because a discovery run reads
    nothing about jobs and writes nothing but proposals.
    """

    def known_slugs(self) -> set[tuple[str, str]]: ...

    def company_aliases(self) -> dict[str, str]: ...

    def insert_proposal(self, proposal: SourceProposal, now: datetime) -> int | None: ...


class SearchProvider(Protocol):
    """Web search. A real provider plus a recorded fake, which is why it is a port."""

    def search(self, query: str, max_results: int) -> Sequence[SearchResult]: ...


class CompanyListProvider(Protocol):
    """An external, user-curated watchlist. Fetches names, not postings — a
    `JobSource` fetches jobs for a board already identified; this fetches an unordered
    list of company names still to be resolved to one."""

    def fetch(self) -> Sequence[CompanyRef]: ...


class CompanyListStore(Protocol):
    """The slice of the store a company-list sync touches: the resolution ledger and
    the target list it feeds, straight into the active crawl on resolution."""

    def upsert_import_company(self, list_source: str, ref: CompanyRef, now: datetime) -> None: ...

    def get_import_company(self, list_source: str, external_id: str) -> ImportCompany | None: ...

    def record_manual_resolution(
        self,
        list_source: str,
        external_id: str,
        board_type: str,
        slug: str,
        open_roles: int | None,
        now: datetime,
    ) -> None: ...

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
    ) -> None: ...

    def insert_imported_target(
        self, target: SourceTarget, now: datetime, external_id: str
    ) -> None: ...

    def deactivate_missing_import_companies(
        self, list_source: str, seen_external_ids: Iterable[str], now: datetime
    ) -> tuple[str, ...]: ...


class SampleRoleStore(Protocol):
    """Postings the user holds up as "roles like this", and the crawl they extend: a
    sample role's company board is watched from then on."""

    def add_sample_role(self, role: SampleRole, now: datetime) -> tuple[SampleRole, bool]: ...

    def active_sample_roles(self) -> list[SampleRole]: ...

    def archive_sample_role(self, role_id: int, now: datetime) -> bool: ...

    def add_sample_role_target(self, source: str, slug: str, now: datetime) -> None: ...


class SearchProfileStore(Protocol):
    """Profiles cached by a hash of the intent log, sample roles and CV they read."""

    def search_profile(self, inputs_hash: str) -> SearchProfile | None: ...

    def latest_search_profile(self) -> SearchProfile | None: ...

    def save_search_profile(
        self, inputs_hash: str, profile: SearchProfile, model: str, now: datetime
    ) -> None: ...


class Notifier(Protocol):
    """Delivers a rendered digest."""

    def deliver(self, title: str, body: str) -> None: ...


class SecretStoreUnavailable(RuntimeError):
    """No place to keep a secret: the OS keychain is missing, or the store is read-only."""


class SecretStore(Protocol):
    """Where API keys live (spec 014). Never SQLite, never a file the app writes."""

    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...  # raises SecretStoreUnavailable

    def delete(self, name: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ApplicationEntry:
    """One row of the user's Applications log. `index` is `applications.id`."""

    index: int
    role: str
    company: str
    source: str
    pilot_score: int | None
    why: str
    applied_on: date
    comments: str
    status: str  # "Applied"
    job_url: str
    location: str


@dataclass(frozen=True, slots=True)
class NotionResult:
    """Where the row landed. `created` is False when a row for the job URL already existed."""

    page_id: str
    database_id: str
    parent_page_id: str
    created: bool


class ApplicationLog(Protocol):
    """Writes an applied job to an external log. Raises with a user-facing message."""

    def record(self, entry: ApplicationEntry) -> NotionResult: ...
