// Packaged, the API and this build are served from one origin, so the base is empty.
// `next dev` runs on 3000 while uvicorn holds 8000, which is the one case that needs a
// host. NEXT_PUBLIC_API_URL overrides both.
const BASE =
  process.env.NEXT_PUBLIC_API_URL ??
  (process.env.NODE_ENV === "development" ? "http://127.0.0.1:8000" : "");

export type CefrLevel = "A1" | "A2" | "B1" | "B2" | "C1" | "C2" | "native";

/** What the API discloses about the redaction boundary (design doc §12).
 *
 * `redactedPreview` holds the full redacted resume text, not a snippet. It is `null` when
 * there is none to show — redaction is switched off, or detection failed on this file. It is
 * never the original resume text, so a `null` must be rendered as "no text", never as an
 * empty document.
 */
export type OwnerAnchor = "confirmed" | "likely" | "none";

export type ResumePrivacy = {
  enabled: boolean;
  /** Distinct values replaced per category, e.g. `{ PERSON: 3, EMAIL: 1 }`. */
  counts: Record<string, number>;
  /** The full redacted resume text, not a truncated preview. */
  redactedPreview: string | null;
  /** How well the owner's identity could be derived from the resume. A level only: the
   * derived name, email and phone are never sent back in this payload. */
  ownerAnchor: OwnerAnchor;
  /** Why there is no preview, when there is none — a failure class or a named missing
   * model, never text or a detected value, so it is safe to show. */
  unavailableReason: string | null;
};

export type ResumeDetections = {
  filename: string;
  parsedAt: string;
  /** The stored CV version this describes. Re-uploading identical text keeps the version. */
  version: number;
  sha256: string;
  /** False when an upload matched the CV that was already current, or on a plain read. */
  changed: boolean;
  summary: { roles: number; skills: number; languages: number };
  privacy: ResumePrivacy;
  detected: {
    roles: string[];
    skills: string[];
    languages: { name: string; level: CefrLevel | null }[];
  };
};

// The copy for each documented failure lives with the screen, keyed by status,
// so a message never depends on what the server happens to phrase.
const MESSAGES: Record<number, string> = {
  413: "That file is over the 5 MB limit.",
  415: "Only PDF and DOCX files are supported.",
  422: "Could not read any text from that file.",
};
const FALLBACK = "Something went wrong reading that file.";

// A reachability problem is not a parsing problem, so it gets its own copy
// rather than borrowing a message about the file.
export const UNREACHABLE =
  "Could not reach the resume service. Is the API running?";

export function uploadMessage(status: number): string {
  return MESSAGES[status] ?? FALLBACK;
}

/** The parsed resume, or null when the server holds none. Throws if it cannot say. */
export async function getResume(): Promise<ResumeDetections | null> {
  const response = await fetch(`${BASE}/api/resume`);
  if (response.status === 404) return null;
  if (!response.ok) throw new Error(UNREACHABLE);
  return (await response.json()) as ResumeDetections;
}

export async function uploadResume(file: File): Promise<ResumeDetections> {
  const body = new FormData();
  body.append("file", file);
  const response = await fetch(`${BASE}/api/resume`, { method: "POST", body });
  if (!response.ok) throw new Error(uploadMessage(response.status));
  return (await response.json()) as ResumeDetections;
}

export async function deleteResume(): Promise<void> {
  const response = await fetch(`${BASE}/api/resume`, { method: "DELETE" });
  if (!response.ok) throw new Error(UNREACHABLE);
}

/** A value the form edits. The server keeps `term` — the lowercase string a posting is
 * matched against — and sends `label` purely so the chip can be displayed properly. */
export type Term = {
  term: string;
  label: string;
  /** True while this value came from the uploaded resume rather than the saved form. */
  fromResume: boolean;
};

export type PreferenceLanguage = {
  name: string;
  level: CefrLevel;
  fromResume: boolean;
};

export type Preferences = {
  /** 0 means nothing has been saved yet: the values are derived from config.yaml and,
   * where a resume was uploaded, its detections. Any other number is a saved version. */
  version: number;
  updatedAt: string | null;
  /** What the user is looking for, in their own words. Prompt material only: no rule reads
   * it, and nothing here is matched against a posting. */
  roleIntent: string;
  roleFamilies: Term[];
  /** The positive role gate: a posting whose title matches none of these is parked for
   * manual review rather than shown. An empty list turns the gate off. */
  titleTerms: Term[];
  coreSkills: Term[];
  excludedStack: Term[];
  excludedRoleTerms: Term[];
  seniority: string[];
  allowedCities: Term[];
  preferredCities: Term[];
  flaggedCities: Term[];
  allowRemote: boolean;
  requirePermanent: boolean;
  languages: PreferenceLanguage[];
  maxPerCompany: number;
  batchSize: number;
  notes: string;
  /** The vocabularies the server accepts, so the screen never hardcodes its own copy. */
  seniorityBands: string[];
  cefrLevels: CefrLevel[];
};

export type RefilterReport = {
  examined: number;
  changed: number;
  /** Keyed "old→new", e.g. `{ "reject→pass": 3 }`. */
  transitions: Record<string, number>;
  nowPassing: number;
};

/** What saving a draft would do to the jobs already stored; nothing is written. */
export type PreferencesPreview = RefilterReport & {
  /** Stored jobs that would move to rejected. */
  hidden: number;
  /** Per excluded role term: stored titles holding the word, and the ones not already rejected. */
  excludedRoleTerms: Record<string, { matches: number; newlyHidden: number }>;
};

export type SaveResult = {
  preferences: Preferences;
  refiltered: RefilterReport;
  reopenedForScoring: number;
};

/** A change the model proposes. Never applied on its own — the user accepts each one. */
export type FieldChange = {
  field: string;
  action: "add" | "remove" | "set";
  value: string;
  label: string;
  rationale: string;
};

export type ExtractionResult = { changes: FieldChange[]; costEur: number };

/** Per-field messages from a 422, keyed by the server's field name. */
export class ValidationError extends Error {
  readonly errors: Record<string, string>;

  constructor(errors: Record<string, string>) {
    super("Some preferences could not be saved.");
    this.name = "ValidationError";
    this.errors = errors;
  }
}

export const PREFERENCES_UNREACHABLE =
  "Could not reach the preferences service. Is the API running?";
export const NO_MODEL_KEY = "The configured model backend is unavailable.";
export const EXTRACT_FAILED =
  "The model could not be reached. Nothing was changed.";

export async function getPreferences(): Promise<Preferences> {
  // A dead API and a 500 are the same thing to this screen: it has no values to edit.
  // Either way the message names the service, not the browser's own error.
  const response = await fetch(`${BASE}/api/preferences`).catch(() => {
    throw new Error(PREFERENCES_UNREACHABLE);
  });
  if (!response.ok) throw new Error(PREFERENCES_UNREACHABLE);
  return (await response.json()) as Preferences;
}

export async function savePreferences(body: unknown): Promise<SaveResult> {
  const response = await fetch(`${BASE}/api/preferences`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (response.status === 422) {
    const detail = (await response.json()) as {
      detail?: { errors?: Record<string, string> };
    };
    throw new ValidationError(detail.detail?.errors ?? {});
  }
  if (!response.ok) throw new Error(PREFERENCES_UNREACHABLE);
  return (await response.json()) as SaveResult;
}

export async function previewPreferences(body: unknown): Promise<PreferencesPreview> {
  const response = await fetch(`${BASE}/api/preferences/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(PREFERENCES_UNREACHABLE);
  return (await response.json()) as PreferencesPreview;
}

export async function extractPreferences(
  notes: string,
  preferences?: Preferences,
): Promise<ExtractionResult> {
  const response = await fetch(`${BASE}/api/preferences/extract`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes, preferences }),
  });
  if (response.status === 409) throw new Error(NO_MODEL_KEY);
  if (!response.ok) throw new Error(EXTRACT_FAILED);
  return (await response.json()) as ExtractionResult;
}

export type RunPreview = {
  candidatesNow: number;
  estimatedCalls: number;
  reservedEur: number;
  ceilingEur: number;
  mode: string;
  token: string;
  modelBackendReady: boolean;
  modelBackendProblem: string;
  /** A cloud (OpenRouter) role has no key: the setup gap, as opposed to a server down. */
  modelBackendKeyMissing?: boolean;
  /** What the page offers when a local backend is down. Never applied for the user. */
  modelBackendSuggest?: "hybrid" | "cloud" | "add_key" | null;
};
export type PipelineRun = {
  runId: number;
  state: "running" | "syncing" | "succeeded" | "aborted" | "failed";
  kind: string;
  stage: string;
  completed: number;
  total: number;
  costEur: number;
  budgetEur: number;
  stalled: boolean;
  failureCategory: string | null;
  failureMessage: string | null;
  sourceFailures: { source: string; identifier: string; message: string }[];
  startedAt: string;
  endedAt: string | null;
  summary?: string | null;
  companyCounts?: CompanySyncCounts | null;
  stages?: RunStage[];
  funnel?: RunFunnel | null;
};
export type RunStage = {
  stage: string;
  startedAt: string;
  endedAt: string | null;
  seconds: number | null;
};
export type RunFunnel = {
  discover: {
    targets: number;
    searched: number;
    fetched: number;
    inserted: number;
    rejected: number;
    review: number;
    qualified: number;
    rejectReasons?: { reason: string; count: number }[];
    boards: { target: string; fetched: number }[];
    skipped: { target: string; reason: string }[];
    failed: { target: string; error: string }[];
  };
  score: {
    candidates: number;
    capped: number;
    matched: number;
    above: number;
    target: number;
    minScore: number;
    failed: number;
    stoppedBy: string;
  };
};
export type CompanySyncCounts = {
  fetched: number;
  resolved: number;
  unresolved: number;
  blocked: number;
  deactivated: number;
};
export type JobResult = {
  id: number;
  title: string;
  company: string;
  location: string;
  source: string;
  url: string;
  firstSeenAt: string;
  outcome: string;
  triageScore: number | null;
  matchScore: number | null;
  costEur: number | null;
};
export type JobsPage = {
  items: JobResult[];
  total: number;
  page: number;
  limit: number;
};

async function runsRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}/api/${path}`, {
    cache: "no-store",
    ...init,
  }).catch(() => {
    throw new Error(
      "Could not reach the run service. Check that the API is running.",
    );
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { errors?: Record<string, string> };
    };
    throw new Error(
      Object.values(body.detail?.errors ?? {}).join(" ") ||
        "The request failed. Try again.",
    );
  }
  return (await response.json()) as T;
}
export const previewRun = () =>
  runsRequest<RunPreview>("runs/preview", { method: "POST" });
export const getRuns = () => runsRequest<PipelineRun[]>("runs");
export const getRun = (id: number) => runsRequest<PipelineRun>(`runs/${id}`);
export const startRun = (preview: RunPreview) =>
  runsRequest<{ runId: number; state: string }>("runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token: preview.token,
      ceilingEur: preview.ceilingEur,
    }),
  });
/** One role's backend readiness, as `GET /api/health/models` reports it. `local` is true for
 * ollama and vllm: a run on that role costs nothing. */
export type ModelRole = {
  role: string;
  kind: string;
  model: string;
  base_url: string | null;
  reachable: boolean;
  model_present: boolean;
  tools: boolean | null;
  detail: string;
  local: boolean;
};
export const getModelHealth = () =>
  runsRequest<{ ok: boolean; roles: ModelRole[] }>("health/models").then((body) => body.roles);
/** Spending approval applies only when some role is paid. The server gate on starting a run
 * holds either way; this decides only whether the page offers the approval card. */
export const needsApproval = (roles: ModelRole[]) => roles.some((role) => !role.local);
/** Ask for an automatic refresh. The server decides: it only starts free (local-model)
 * runs, and with `ifStale` only when the last good run is over 20 hours old. */
export type RefreshResult = {
  started: boolean;
  runId?: number;
  reason?: string;
  /** `billed_roles`: a paid model is configured, so the run needs preview and approval. */
  code?: string;
};
export const BILLED_ROLES = "billed_roles";
/** The privacy notice shown before a run that sends text to a cloud provider. */
export type CloudNotice = {
  show: boolean;
  redactionEnabled: boolean;
  providers: string[];
};
export const getCloudNotice = () => runsRequest<CloudNotice>("runs/cloud-notice");
/** `remind` false means never show the notice again. */
export const dismissCloudNotice = (remind: boolean) =>
  runsRequest<{ show: boolean }>("runs/cloud-notice", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ remind }),
  });
export const refreshRun = (ifStale: boolean) =>
  runsRequest<RefreshResult>("runs/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ifStale }),
  });
export const getJobs = (
  outcome: string,
  sort: string,
  direction: string,
  page: number,
) => {
  const query = new URLSearchParams({ sort, direction, page: String(page) });
  if (outcome) query.set("outcome", outcome);
  return runsRequest<JobsPage>(`jobs?${query}`);
};

// --- company lists ----------------------------------------------------------------------

// A curated watchlist's connection state: which source (if any) is configured, and
// whether the Notion token has been set in `.env` server-side (it is never round-tripped
// to this client — only whether it's present).
export type CompanyListConfig = {
  notion: { databaseId: string };
  csv: { path: string };
  configured: "notion" | "csv" | null;
  notionKeyConfigured: boolean;
};

const COMPANY_LISTS_UNREACHABLE =
  "Could not reach the company-list service. Is the API running?";

async function companyListsRequest<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`${BASE}/api/company-lists/${path}`, {
    cache: "no-store",
    ...init,
  }).catch(() => {
    throw new Error(COMPANY_LISTS_UNREACHABLE);
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { errors?: Record<string, string> };
    };
    throw new Error(
      Object.values(body.detail?.errors ?? {}).join(" ") ||
        "The request failed. Try again.",
    );
  }
  return (await response.json()) as T;
}

export const getCompanyListConfig = () =>
  companyListsRequest<CompanyListConfig>("config");

export const saveCompanyListConfig = (databaseId: string, csvPath: string) =>
  companyListsRequest<CompanyListConfig>("config", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notion: { databaseId }, csv: { path: csvPath } }),
  });

export async function uploadCompanyCsv(
  file: File,
): Promise<{ path: string; companies: number }> {
  const body = new FormData();
  body.append("file", file);
  return companyListsRequest<{ path: string; companies: number }>("csv", {
    method: "POST",
    body,
  });
}

export type CompanyListSummary = {
  listSource: "notion" | "csv" | null;
  counts: {
    resolved: number;
    unresolved: number;
    blocked: number;
    inactive: number;
  };
  openRoles: number;
  foundWithoutRoles: number;
  companies: {
    id: number;
    name: string;
    /** The user named this company's board by hand; a sync re-checks it, never replaces it. */
    pinned: boolean;
    /** Added by name through "Add a company": in the crawl but not in the list. */
    manual?: boolean;
    status: "resolved" | "unresolved" | "blocked" | "inactive";
    boardType: string | null;
    openRoles: number | null;
    lastError: string | null;
  }[];
  failures: { name: string; status: string; lastError: string }[];
};

export const getCompanyListSummary = () =>
  companyListsRequest<CompanyListSummary>("summary");

/** Point a company the resolver missed at its real board: a careers-page link or
 * `vendor/slug`. The server checks it against the live board first, so a wrong answer
 * rejects with a message that says why, and nothing changes. */
export type BoardLookup = {
  name: string;
  found: boolean;
  boardType: string | null;
  slug: string | null;
  openRoles: number | null;
  tried: string[];
  /** Set when a board may exist but could not be checked: try again later. */
  blockedReason: string | null;
  /** True only when the board was not found and web search is configured. */
  canSearchWeb: boolean;
};

export type FoundBoard = {
  boardType: string;
  slug: string;
  company?: string;
  openRoles: number | null;
  sampleTitles?: string[];
  evidence?: string;
};

/** Guess a company's job board from its name. Changes nothing. */
export const lookupCompany = (name: string) =>
  post<BoardLookup>("sources/lookup", { name });

/** Ask the discovery agent for one company's board. Spends discovery budget, so the page
 * calls it only after the user said yes. */
export const searchCompanyOnWeb = (name: string) =>
  post<{ name: string; boards: FoundBoard[] }>("sources/lookup/search", { name });

/** Add a board to the crawl. The server checks it against the real vendor again. */
export const confirmBoard = (boardType: string, slug: string, name?: string) =>
  post<{
    boardType: string;
    slug: string;
    openRoles: number | null;
    /** The crawl already visited this board; nothing was written. */
    alreadyListed: boolean;
    /** The name of the not-found list entry this board was given to, if any. */
    filledListEntry: string | null;
  }>(
    "sources/lookup/confirm",
    { boardType, slug, name },
  );

/** One board the crawler visits. `origin` says where it came from; `lastVisitedAt` is
 * null until a run has fetched it. */
export type BoardVisit = {
  name: string;
  boardType: string;
  slug: string;
  origin: "imported" | "manual" | "approved" | "sample_role" | "config";
  status: "resolved" | "unresolved" | "blocked" | "inactive";
  addedAt: string | null;
  openRoles: number | null;
  lastVisitedAt: string | null;
};

/** Every board the crawler visits, the one a run reaches next first. */
export const getBoardVisits = () => reviewRequest<BoardVisit[]>("sources/boards");

export const setCompanyBoard = (companyId: number, board: string) =>
  companyListsRequest<{
    id: number;
    status: "resolved";
    boardType: string;
    slug: string;
    openRoles: number | null;
    pinned: true;
  }>(`companies/${companyId}/board`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ board }),
  });

export const syncCompanyList = (from?: "notion" | "csv") =>
  companyListsRequest<{ runId: number; state: string }>("sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(from ? { from } : {}),
  });

// --- review ---------------------------------------------------------------

/** Which pass produced the score a row is ranked by. `null` means no model has read the
 * posting at all, which the screen says out loud rather than showing as a zero. */
export type ScoreStage = "match" | "triage" | null;

export type ReviewRow = {
  id: number;
  title: string;
  company: string;
  /** The company's homepage when known, else its careers board, else null. */
  companyUrl: string | null;
  location: string;
  source: string;
  url: string;
  firstSeenAt: string;
  /** When the crawler last saw the advert on its board. A posting the board has dropped
   * still appears until it is retired, and its link lands on the board's error page. */
  lastSeenAt: string;
  outcome: "pass" | "manual_review" | "reject";
  score: number | null;
  scoreStage: ScoreStage;
  verdict: string | null;
  why: string;
  label: ReviewLabel | null;
  /** The advert changed after the label was given, so the label may no longer apply. */
  labelStale: boolean;
  belowThreshold: boolean;
  /** A remote role posted from another country; ranked below every threshold. */
  locationMismatch: boolean;
  /** The user's current decision about this posting. `dismissed` never reaches a list. */
  decision: JobDecision | null;
  /** Saved postings are pinned on top, with the new ones. */
  isSaved: boolean;
  /** Applied postings stay listed with a badge, below new and saved, and are not rescored. */
  isApplied: boolean;
  /** The job's row in the Notion Applications database, or null when it has none. */
  notionIndex: number | null;
  /** True until the user has been shown the posting. */
  isNew: boolean;
  viewedAt: string | null;
  /** The CV version that produced the score; null for a score from before CVs were
   * versioned. A job scored under an older CV keeps its score when a new CV is uploaded. */
  scoredWithCv: number | null;
};

export type JobDecision = "maybe" | "dismissed" | "saved" | "applied";

export type ReviewPage = {
  items: ReviewRow[];
  /** How long an unseen posting survives before the crawler retires it. The screen ages a
   * row against this rather than against a number of its own. */
  inactiveAfterDays: number;
  threshold: number;
  limit: number;
  /** Rules-kept and scored at or above the threshold. */
  kept: number;
  dropped: number;
  /** Rules-kept but scored below the threshold. */
  below: number;
  /** Rules-kept but not yet scored by any model. */
  unscored: number;
  total: number;
  /** Dismissed jobs still in the recycle bin, for the "Recycle bin (n)" link. */
  binCount: number;
};

export type ReviewLabel = "good_fit" | "maybe" | "not_relevant";

export type PartName = "skills" | "seniority" | "domain" | "preferences";
/** The four named parts behind a match score, each with the sentence that says why. */
export type ScoreParts = Record<PartName, { points: number; reason: string }>;

export type JobCard = ReviewRow & {
  /** Null for a score from before breakdowns existed (rubric 1) and for a job no model
   * has matched yet. */
  parts: ScoreParts | null;
  description: string;
  employmentType: string | null;
  remote: boolean;
  lastSeenAt: string;
  contentHash: string;
  filterReasons: string[];
  filterWarnings: string[];
  triageScore: number | null;
  triageReasons: string[];
  matchScore: number | null;
  reasons: string[];
  gaps: string[];
  warnings: string[];
  /** The verbatim resume quote behind each claim, in claim order. */
  evidence: string[];
  rubricVersion: number | null;
  model: string | null;
  scoredAt: string | null;
  costEur: number | null;
  note: string | null;
  labelledAt: string | null;
};

export const REVIEW_UNREACHABLE =
  "Could not reach the review service. Is the API running?";

async function reviewRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}/api/${path}`, {
    cache: "no-store",
    ...init,
  }).catch(() => {
    throw new Error(REVIEW_UNREACHABLE);
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { errors?: Record<string, string> };
    };
    throw new Error(
      Object.values(body.detail?.errors ?? {}).join(" ") ||
        "The request failed. Try again.",
    );
  }
  return (await response.json()) as T;
}

export const getReview = (
  threshold: number,
  includeBelow: boolean,
  limit = 10,
  decision?: "saved" | "applied",
) =>
  reviewRequest<ReviewPage>(
    `review?threshold=${threshold}&includeBelow=${includeBelow ? "true" : "false"}&limit=${limit}` +
      (decision ? `&decision=${decision}` : ""),
  );

export const getJob = (id: number) => reviewRequest<JobCard>(`jobs/${id}`);

/** A posting's responsibilities and requirements, copied from its text. `unavailable`
 * means the model could not read it; the card then points at the original posting. */
export type JobOutline =
  | { status: "ok"; responsibilities: string[]; requirements: string[] }
  | { status: "unavailable" };

/** Read once on a card's first open, then kept by the server. */
export const getJobOutline = (id: number) =>
  reviewRequest<JobOutline>(`jobs/${id}/outline`, { method: "POST" });

const post = <T>(path: string, body: unknown) =>
  reviewRequest<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

/** Tell the server these postings were shown, so they stop being new. Called after the
 * cards render, never as a side effect of reading the list. */
export const markViewed = (ids: number[]) =>
  post<{ marked: number }>("jobs/viewed", { ids });

/** Append-only. A label also sets the decision (good fit saves, not relevant dismisses,
 * maybe keeps), so a dismissed posting does not come back. Nothing is re-ranked by it. */
export const saveReview = (jobId: number, label: ReviewLabel, note = "") =>
  post<{ reviewId: number; decision: JobDecision; rerankedNothing: boolean }>(
    "reviews",
    {
      jobId,
      label,
      note,
    },
  );

export const saveCorrection = (
  jobId: number,
  kind: "evidence" | "rule",
  target: string,
) => post<{ correctionId: number }>("corrections", { jobId, kind, target });

// --- feedback assistant ---------------------------------------------------

export type AssistantAction =
  | { kind: "set_view"; limit: number; includeBelow: boolean }
  | { kind: "intent_added"; text: string }
  | { kind: "refresh"; started: boolean; reason?: string; runId?: number }
  | { kind: "jobs_changed"; terms: string[]; count: number };

export type AssistantTurn = { role: "user" | "assistant"; text: string };

export type AssistantReply = {
  text: string;
  actions: AssistantAction[];
  /** A search was running, so the shared local model may have answered slowly. */
  busy?: boolean;
};

/** One chat message. The server keeps no chat state, so the page sends the last turns and
 * the view it is showing. A 409 means the assistant's model is not local; its message is
 * the error text. */
export const sendAssistant = (
  message: string,
  history: AssistantTurn[],
  view: { limit: number; includeBelow: boolean },
) => post<AssistantReply>("assistant", { message, history, view });

/** Writes the outcome of a search the chat started into the thread, so the assistant's
 * own history knows how it ended. */
export const saveAssistantNote = (text: string) =>
  post<{ saved: boolean }>("assistant/note", { text });

// --- applied and notion ----------------------------------------------------

/** Why a job was dismissed. Optional; the server rejects anything else. */
export const DISMISS_REASONS = [
  { value: "wrong_role", label: "Wrong role" },
  { value: "seniority", label: "Seniority" },
  { value: "location", label: "Location" },
  { value: "company", label: "Company" },
  { value: "other", label: "Other" },
] as const;
export type DismissReason = (typeof DISMISS_REASONS)[number]["value"];

/** Append-only, like a label. Applied always saves; Notion is a separate, optional step.
 * A reason belongs to a dismissal and feeds what the match step learns from. */
export const saveDecision = (
  jobId: number,
  decision: JobDecision,
  reason?: DismissReason,
) =>
  post<{ decisionId: number; decision: JobDecision }>(
    `jobs/${jobId}/decision`,
    reason ? { decision, reason } : { decision },
  );

/** Puts back the decision that stood before Applied (null: there was none). A Notion row
 * is left where it is. */
export const undoApplied = (jobId: number) =>
  post<{ decision: JobDecision | null }>(`jobs/${jobId}/undo-applied`, {});

export type NotionAdd = { status: "added" | "already"; index: number };

/** Adds the job as a row in the Notion Applications database, once. A failure throws
 * with the reason in plain words; the job stays Applied either way. */
export const addToNotion = (jobId: number) =>
  post<NotionAdd>(`jobs/${jobId}/notion`, {});

// --- intent log and sample roles ------------------------------------------

/** One thing the user said they want, in their own words. A `condensed` entry stands in
 * for older entries the profiler has folded together. */
export type IntentEntry = {
  id: number;
  text: string;
  kind: "entry" | "condensed";
  createdAt: string;
};

export type IntentLog = {
  entries: IntentEntry[];
  /** Words across the entries still in play, counted by the server. */
  activeWords: number;
  /** The length at which the log is worth condensing. */
  condenseAt: number;
};

/** A posting the user points at as "more like this". `url` is null for pasted text. */
export type SampleRole = {
  id: number;
  company: string;
  title: string;
  source: string;
  url: string | null;
  addedAt: string;
};

/** A link the server could not use; the screen should offer the paste-text form. */
export class NeedsTextError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "NeedsTextError";
  }
}

export const INTENT_UNREACHABLE =
  "Could not reach the intent service. Is the API running?";

/** The condenser model failed and the server did not say why. */
export const CONDENSE_FAILED =
  "The model could not draft a condensed note. Nothing was changed.";

type Refusal = {
  detail?: { errors?: Record<string, string>; needsText?: boolean };
};

/** One request to the intent or sample-role routes. A 422 becomes a `ValidationError`
 * carrying the server's field messages, or a `NeedsTextError` when the server says the
 * link it was given cannot be read. A 502 is the model behind the route failing, and
 * carries the server's reason. Anything else that is not ok is a reachability problem
 * as far as this screen can tell. */
async function intentRequest(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const response = await fetch(`${BASE}/api/${path}`, {
    cache: "no-store",
    ...init,
  }).catch(() => {
    throw new Error(INTENT_UNREACHABLE);
  });
  if (response.status === 422) {
    const body = (await response.json().catch(() => ({}))) as Refusal;
    const errors = body.detail?.errors ?? {};
    if (body.detail?.needsText) {
      throw new NeedsTextError(
        errors.url ??
          "That link could not be read. Paste the posting's text instead.",
      );
    }
    throw new ValidationError(errors);
  }
  if (response.status === 502) {
    const body = (await response.json().catch(() => ({}))) as Refusal;
    throw new Error(body.detail?.errors?.model ?? CONDENSE_FAILED);
  }
  if (!response.ok) throw new Error(INTENT_UNREACHABLE);
  return response;
}

const sendJson = (path: string, body: unknown) =>
  intentRequest(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

/** Removing something already gone leaves the screen where it wanted to be, so a 404
 * counts as done rather than as an outage. */
async function remove(path: string): Promise<void> {
  const response = await fetch(`${BASE}/api/${path}`, {
    method: "DELETE",
  }).catch(() => {
    throw new Error(INTENT_UNREACHABLE);
  });
  if (!response.ok && response.status !== 404)
    throw new Error(INTENT_UNREACHABLE);
}

export async function getIntent(): Promise<IntentLog> {
  return (await (await intentRequest("intent")).json()) as IntentLog;
}

export async function addIntent(text: string): Promise<IntentEntry> {
  return (await (await sendJson("intent", { text })).json()) as IntentEntry;
}

export const removeIntent = (id: number) => remove(`intent/${id}`);

/** A draft note folding the active entries together. Nothing is stored until approved. */
export async function condenseIntent(): Promise<string> {
  const body = (await (await sendJson("intent/condense", {})).json()) as {
    draft: string;
  };
  return body.draft;
}

/** Swap the approved note in for every active entry; returns the log as it now stands. */
export async function approveCondensed(text: string): Promise<IntentLog> {
  return (await (
    await sendJson("intent/condense/approve", { text })
  ).json()) as IntentLog;
}

export async function getSampleRoles(): Promise<SampleRole[]> {
  const body = (await (await intentRequest("sample-roles")).json()) as {
    roles: SampleRole[];
  };
  return body.roles;
}

export async function addSampleRoleUrl(url: string): Promise<SampleRole> {
  return (await (await sendJson("sample-roles", { url })).json()) as SampleRole;
}

export async function addSampleRoleText(
  title: string,
  text: string,
  company?: string,
): Promise<SampleRole> {
  const body = company ? { title, text, company } : { title, text };
  return (await (await sendJson("sample-roles", body)).json()) as SampleRole;
}

export const removeSampleRole = (id: number) => remove(`sample-roles/${id}`);

export type ObsTiles = {
  tokensPerS: number;
  avgLatencyS: number;
  totalTokens: number;
  calls: number;
  errorRate: number;
};

export type ObsModelRow = {
  model: string;
  backend: string;
  stage: string;
  calls: number;
  tokensIn: number;
  tokensOut: number;
  tokensPerSP50: number | null;
  latencyP50: number | null;
  latencyP95: number | null;
};

export type ObsSummary = { days: number; tiles: ObsTiles; rows: ObsModelRow[] };

export type ObsCost = {
  days: number;
  totalEur: number;
  byBackend: { backend: string; eur: number; calls: number }[];
  byModel: {
    model: string;
    backend: string;
    stage: string;
    eur: number;
    calls: number;
  }[];
  byDay: { day: string; eur: number }[];
};

export type ObsToolRow = {
  agent: string;
  tool: string;
  calls: number;
  slowestS: number | null;
  refused: number;
  errors: number;
};

export type ObsFlowStep = {
  tool: string;
  outcome: string;
  toolS: number;
  stepS: number;
};

export type ObsFlow = {
  agent: string;
  model: string;
  outcome: string;
  requests: number;
  tokensIn: number | null;
  tokensOut: number | null;
  startedAt: string;
  totalS: number;
  steps: ObsFlowStep[];
  endS: number;
};

export type ObsSeriesPoint = { day: string; model: string; tokensPerS: number };

export type ObsSession = {
  id: number;
  startedAt: string;
  endedAt: string | null;
  durationS: number | null;
  outcome: string | null;
  profile: string | null;
  logPath: string;
  hasLog: boolean;
};

async function observabilityRequest<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}/api/observability/${path}`, {
    cache: "no-store",
  }).catch(() => {
    throw new Error(
      "Could not reach the observability service. Check that the API is running.",
    );
  });
  if (!response.ok) throw new Error("The request failed. Try again.");
  return (await response.json()) as T;
}

export const getObsSummary = (days: number) =>
  observabilityRequest<ObsSummary>(`summary?days=${days}`);
export const getObsCost = (days: number) =>
  observabilityRequest<ObsCost>(`cost?days=${days}`);
export const getObsTools = (days: number) =>
  observabilityRequest<ObsToolRow[]>(`tools?days=${days}`);
export const getObsSeries = (days: number) =>
  observabilityRequest<ObsSeriesPoint[]>(`series?days=${days}`);
export const getObsSessions = () =>
  observabilityRequest<ObsSession[]>("sessions");
export const getObsFlows = (days: number, limit?: number) =>
  observabilityRequest<ObsFlow[]>(`flows?days=${days}${limit ? `&limit=${limit}` : ""}`);
export const obsLogUrl = (sessionId: number) =>
  `${BASE}/api/observability/sessions/${sessionId}/log`;

// --- job sources -------------------------------------------------------------

export type SourceStatus = "ok" | "degraded" | "paused" | "unknown";

/** One job source (a vendor or service) as `GET /api/sources/health` reports it. The
 * `lastMessage` is the reason in words, never a raw error, so it is safe to show. */
export type SourceHealthRow = {
  source: string;
  status: SourceStatus;
  targets: number;
  lastOkAt: string | null;
  lastFailure: string | null;
  lastMessage: string | null;
  pausedUntil: string | null;
  fetchedLast7Days: number;
};

export type SourceTestResult = {
  ok: boolean;
  fetched: number;
  durationMs: number;
  reason: string | null;
  message: string | null;
};

async function sourcesRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}/api/sources/${path}`, {
    cache: "no-store",
    ...init,
  }).catch(() => {
    throw new Error("Could not reach the API. Check that it is running.");
  });
  if (!response.ok) {
    // The server words the limit ("tested a moment ago; try again in 42 s"), so show it.
    const body = (await response.json().catch(() => null)) as {
      detail?: { errors?: { source?: string } };
    } | null;
    throw new Error(body?.detail?.errors?.source ?? "The request failed. Try again.");
  }
  return (await response.json()) as T;
}

export const getSourceHealth = () => sourcesRequest<SourceHealthRow[]>("health");
export const testSource = (name: string) =>
  sourcesRequest<SourceTestResult>(`${encodeURIComponent(name)}/test`, { method: "POST" });
export const resumeSource = (name: string) =>
  sourcesRequest<{ status: SourceStatus }>(`${encodeURIComponent(name)}/resume`, {
    method: "POST",
  });

// --- recycle bin -------------------------------------------------------------

/** A dismissed job still in the bin. After `daysLeft` it is cleaned and gone for good. */
export type BinItem = {
  jobId: number;
  title: string;
  company: string;
  dismissedAt: string;
  daysLeft: number;
};

export type RecycleBin = { days: number; items: BinItem[] };

export const getBin = () => reviewRequest<RecycleBin>("bin");

/** Take a job out of the bin; it is listed, and scored if needed, again. */
export const restoreJob = (jobId: number) =>
  reviewRequest<{ jobId: number; decision: null }>(`jobs/${jobId}/restore`, {
    method: "POST",
  });

/** Clean the job now, as the bin's time limit would. Cannot be undone. */
export async function deleteForever(jobId: number): Promise<void> {
  const response = await fetch(`${BASE}/api/bin/${jobId}`, {
    method: "DELETE",
    cache: "no-store",
  }).catch(() => {
    throw new Error(REVIEW_UNREACHABLE);
  });
  if (!response.ok) throw new Error("The request failed. Try again.");
}

// --- model settings (spec 014) ---------------------------------------------------------

export type Preset = "local" | "cloud" | "hybrid";
export type RoleOverride = { backend: string; model: string; reserveEur?: number | null };
export type ResolvedModelRole = {
  role: string;
  kind: string;
  model: string;
  source: "preset" | "override" | "config";
  local: boolean;
  needs: string;
};
export type KeyStatus = {
  set: boolean;
  source: "env" | "keychain" | "session" | "none";
  tail: string | null;
  keychain: boolean;
};
export type ModelSettingsView = {
  settings: {
    version: number;
    preset: Preset | null;
    overrides: Record<string, RoleOverride>;
    cloudConsent: boolean;
    savedAt: string | null;
  };
  profile: { name: string | null; source: string; reason: string };
  envProfile: string | null;
  roles: ResolvedModelRole[];
  backends: string[];
  key: KeyStatus;
  budget: { maxEurPerRun: number; lastRunEur: number | null };
};
export type KeyTest = { valid: boolean; limit: number | null; usage: number | null; detail: string };
export type CatalogModel = {
  id: string;
  tier: "recommended" | "challenger" | "baseline";
  note: string;
  reserveEur: number | null;
  priceIn: number | null;
  priceOut: number | null;
  dynamic: boolean;
  available: boolean | null;
};
export type LiveModelRow = {
  id: string;
  name: string;
  priceIn: number | null;
  priceOut: number | null;
  dynamic: boolean;
};

/** A failed settings call, with its status: 409 means "add a key", 428 "accept the notice". */
export class SettingsError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function settingsRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}/api/settings/${path}`, {
    cache: "no-store",
    ...(init?.body ? { headers: { "Content-Type": "application/json" } } : {}),
    ...init,
  }).catch(() => {
    throw new SettingsError("Could not reach the API. Check that it is running.", 0);
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as {
      detail?: { errors?: Record<string, string> };
    };
    throw new SettingsError(
      Object.values(body.detail?.errors ?? {}).join(" ") || "The request failed. Try again.",
      response.status,
    );
  }
  return (await response.json()) as T;
}

export const getModelSettings = () => settingsRequest<ModelSettingsView>("models");
export const saveModelSettings = (body: {
  preset: Preset | null;
  overrides: Record<string, RoleOverride>;
  cloudConsent?: boolean;
}) => settingsRequest<ModelSettingsView>("models", { method: "PUT", body: JSON.stringify(body) });
export const testOpenRouterKey = (key: string) =>
  settingsRequest<KeyTest>("openrouter-key/test", { method: "POST", body: JSON.stringify({ key }) });
export const saveOpenRouterKey = (key: string) =>
  settingsRequest<KeyStatus>("openrouter-key", { method: "PUT", body: JSON.stringify({ key }) });
export const clearOpenRouterKey = () =>
  settingsRequest<KeyStatus>("openrouter-key", { method: "DELETE" });
export const getOpenRouterModels = (role: string) =>
  settingsRequest<{ catalog: CatalogModel[]; models: LiveModelRow[]; liveError: string }>(
    `openrouter-models?role=${encodeURIComponent(role)}`,
  );

// --- accounts (spec 015) ----------------------------------------------------------

export type AuthStatus = {
  accounts: string[];
  user: string | null;
  /** True while no account exists yet and a database from before accounts is on disk. */
  adoptsExisting: boolean;
};

export async function getAuthStatus(): Promise<AuthStatus> {
  const response = await fetch(`${BASE}/api/auth/status`, { cache: "no-store" });
  if (!response.ok) throw new Error("Could not reach the server. Try again.");
  return (await response.json()) as AuthStatus;
}

async function authMessage(response: Response, fallback: string): Promise<string> {
  const body = (await response.json().catch(() => ({}))) as { detail?: unknown };
  return typeof body.detail === "string" ? body.detail : fallback;
}

/** Sign in or create an account. Throws the server's own words for a refusal. */
export async function authenticate(
  mode: "login" | "register",
  name: string,
  passcode: string,
): Promise<void> {
  const response = await fetch(`${BASE}/api/auth/${mode}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, passcode }),
  }).catch(() => {
    throw new Error("Could not reach the server. Try again.");
  });
  if (!response.ok) {
    throw new Error(await authMessage(response, "That did not work. Try again."));
  }
}

/** Create an account whose data is an exported database file. */
export async function importAccount(name: string, passcode: string, file: File): Promise<void> {
  const body = new FormData();
  body.append("name", name);
  body.append("passcode", passcode);
  body.append("file", file);
  const response = await fetch(`${BASE}/api/auth/import`, { method: "POST", body }).catch(() => {
    throw new Error("Could not reach the server. Try again.");
  });
  if (!response.ok) {
    throw new Error(await authMessage(response, "That did not work. Try again."));
  }
}

/** Delete the signed-in account and all its data. Resolves with a refusal, or null once gone. */
export async function deleteAccount(passcode: string): Promise<string | null> {
  const response = await fetch(`${BASE}/api/auth/account`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ passcode }),
  }).catch(() => null);
  if (response === null) return "Could not reach the server. Try again.";
  if (response.ok) return null;
  return authMessage(response, "Could not delete the account. Try again.");
}

/** Resolves with a refusal message (a run is active), or null once signed out. */
export async function signOut(): Promise<string | null> {
  const response = await fetch(`${BASE}/api/auth/logout`, { method: "POST" }).catch(
    () => null,
  );
  if (response === null) return "Could not reach the server. Try again.";
  if (response.ok) return null;
  return authMessage(response, "Could not sign out. Try again.");
}
