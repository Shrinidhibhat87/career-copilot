"use client";

import Link from "next/link";
import {
  Fragment,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";

import { AssistantChat } from "@/components/assistant-chat";
import { LocationFilter } from "@/components/location-filter";
import { notionKept } from "@/components/notion-prompt";
import { RefreshJobs } from "@/components/refresh-jobs";
import { ReviewCard } from "@/components/review-card";
import { ReviewDetail } from "@/components/review-detail";
import { age, mayBeClosed } from "@/lib/freshness";
import { citiesOf } from "@/lib/locations";
import {
  getReview,
  markViewed,
  DISMISS_REASONS,
  type DismissReason,
  saveDecision,
  saveReview,
  undoApplied,
  type AssistantAction,
  type JobDecision,
  type ReviewLabel,
  type ReviewPage,
  type ReviewRow,
} from "@/lib/api";

const MIN_THRESHOLD = 30;
const MAX_THRESHOLD = 90;
/** What the "Show up to" selector offers; the server refuses anything above the last. */
const LIMIT_STEPS = [10, 25, 50];
const DEFAULT_LIMIT = LIMIT_STEPS[LIMIT_STEPS.length - 1];

/** What a label does server-side; mirrored here so the list updates without a refetch. */
const DECISION_FOR_LABEL: Record<ReviewLabel, JobDecision> = {
  good_fit: "saved",
  not_relevant: "dismissed",
  maybe: "maybe",
};

const NOTICE_FOR_LABEL: Record<ReviewLabel, string> = {
  good_fit: "Saved. It stays pinned at the top of your list.",
  not_relevant: "Dismissed. It will not be shown again.",
  maybe: "Marked maybe. It stays in the list.",
};

/** How long a dismissed row flies to the bin; mirrors `bin-fly` in globals.css. */
const LEAVE_MS = 450;
/** How long the bin link pulses after its count goes up. */
const PULSE_MS = 500;

/** A row on its way to the bin: what it looked like, and where the bin is from it. */
type Leaving = { row: ReviewRow; index: number; dx: number; dy: number };

const prefersReducedMotion = () =>
  typeof window.matchMedia === "function" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const message = (error: unknown) =>
  error instanceof Error ? error.message : "The request failed.";

/** The score, or the reason there is none. A posting the model has not read is not a
 * posting that scored nothing, and the column must not blur the two. */
function scoreLabel(row: ReviewRow) {
  if (row.score === null) return "—";
  return String(row.score);
}

/** The score's colour band: strong, middling, weak. */
function tier(score: number | null) {
  if (score === null) return "none";
  if (score >= 80) return "high";
  if (score >= 65) return "mid";
  return "low";
}

/** What the page lists: an unscored posting is not a recommendation, unless the user saved
 * it, so the lower-matches toggle never brings one in. */
const listed = (items: ReviewRow[]) =>
  items.filter((row) => row.score !== null || row.isSaved || row.isApplied);

export function ReviewScreen() {
  const [page, setPage] = useState<ReviewPage | null>(null);
  const [threshold, setThreshold] = useState(MIN_THRESHOLD);
  const [includeBelow, setIncludeBelow] = useState(false);
  const [limit, setLimit] = useState(DEFAULT_LIMIT);
  const [openJob, setOpenJob] = useState<number | null>(null);
  // Where the page was scrolled when Next/Previous was pressed. The card remounts and its
  // loading frame shrinks the page, so the browser resets the scroll; restore it once the
  // next posting is on screen.
  const scrollBeforeGo = useRef<number | null>(null);
  const pressedBackdrop = useRef(false);
  const [notice, setNotice] = useState("");
  // The job just dismissed, while the "why?" choices are on offer. Optional, never blocks.
  const [askWhy, setAskWhy] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  // Ids that were new when first shown. The server stops calling them new the moment they
  // are marked viewed, but the badge stays for the rest of this visit, so a threshold change
  // does not make it vanish from a row the user has not acted on.
  const [freshIds, setFreshIds] = useState<ReadonlySet<number>>(new Set());
  // The Applied pill narrows the list to applied postings; they are hidden otherwise.
  const [showApplied, setShowApplied] = useState(false);
  // The Interested pill narrows the list to postings the user saved.
  const [onlyInterested, setOnlyInterested] = useState(false);
  const [companyQuery, setCompanyQuery] = useState("");
  // Cities picked in the Location filter; none means every location.
  const [selectedLocations, setSelectedLocations] = useState<string[]>([]);
  // Postings marked applied during this visit stay listed, so the Notion question and the
  // undo confirmation have a pane to appear in, instead of the row vanishing under them.
  const [justApplied, setJustApplied] = useState<ReadonlySet<number>>(new Set());
  // The posting the detail pane shows; the first listed one when none (or a filtered-out
  // one) is chosen.
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // The posting the Notion question is about, asked once right after Applied is saved,
  // in a popover beside that row's Applied button.
  const [applying, setApplying] = useState<number | null>(null);
  // The applied posting whose undo is waiting for a confirmation, beside the same button.
  const [undoing, setUndoing] = useState<number | null>(null);
  // Rows that were just dismissed and are still flying to the bin. They are already gone
  // from `page`, so they are drawn from this copy until the animation ends.
  const [leaving, setLeaving] = useState<readonly Leaving[]>([]);
  const [pulsing, setPulsing] = useState(false);
  const binRef = useRef<HTMLAnchorElement>(null);
  const lastBinCount = useRef(0);

  /** Shows a fetched page, and tells the server about the postings that are new in it. */
  function show(result: ReviewPage) {
    setPage(result);
    setError("");
    const fresh = result.items
      .filter((item) => item.isNew)
      .map((item) => item.id);
    if (fresh.length) {
      setFreshIds((current) => new Set([...current, ...fresh]));
      // After the page is on screen, not before: a failed read must leave them new.
      void markViewed(fresh).catch(() => undefined);
    }
  }

  // `loading` is raised by whatever changed the query, the way the results table does it:
  // an effect that sets state on its own first render costs a cascading render.
  useEffect(() => {
    let disposed = false;
    void getReview(threshold, includeBelow, limit)
      .then((result) => {
        if (!disposed) show(result);
      })
      .catch((failure: unknown) => {
        if (!disposed) setError(message(failure));
      })
      .finally(() => {
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
    };
  }, [threshold, includeBelow, limit]);

  const binCount = page?.binCount ?? 0;
  useEffect(() => {
    const grew = binCount > lastBinCount.current;
    lastBinCount.current = binCount;
    if (!grew || prefersReducedMotion()) return;
    setPulsing(true);
    const timer = setTimeout(() => setPulsing(false), PULSE_MS);
    return () => clearTimeout(timer);
  }, [binCount]);

  /** Where each listed row would have to travel to reach the bin link. Empty when there is
   * nothing to animate: reduced motion, or the row is not on screen. */
  function measureRows(items: ReviewRow[]) {
    const found = new Map<number, Leaving>();
    const bin = binRef.current?.getBoundingClientRect();
    if (!bin || prefersReducedMotion()) return found;
    items.forEach((row, index) => {
      const el = document.querySelector(`[data-job-id="${row.id}"]`);
      if (!el) return;
      const box = el.getBoundingClientRect();
      found.set(row.id, {
        row,
        index,
        dx: bin.left + bin.width / 2 - (box.left + box.width / 2),
        dy: bin.top + bin.height / 2 - (box.top + box.height / 2),
      });
    });
    return found;
  }

  /** Keeps the given rows on screen, flying to the bin, until the animation is over. */
  function flyToBin(flying: Leaving[]) {
    if (!flying.length) return;
    const ids = new Set(flying.map((item) => item.row.id));
    setLeaving((current) => [...current, ...flying]);
    setTimeout(
      () =>
        setLeaving((current) => current.filter((item) => !ids.has(item.row.id))),
      LEAVE_MS,
    );
  }

  /** Reloads the list after a refresh, resolving with the ids it now holds. */
  async function refetch() {
    const result = await getReview(threshold, includeBelow, limit);
    show(result);
    return listed(result.items).map((item) => item.id);
  }

  const rows = listed(page?.items ?? []);
  const available = includeBelow ? (page?.total ?? 0) : (page?.kept ?? 0);
  // Falls back to the server's own default rather than to zero: zero would mark every row
  // as possibly closed during the first paint, before the page has answered.
  const inactiveAfterDays = page?.inactiveAfterDays ?? 21;
  const canShowMore = rows.length >= limit && available > rows.length;

  function remember(jobId: number, label: ReviewLabel) {
    const decision = DECISION_FOR_LABEL[label];
    if (decision === "dismissed") {
      const gone = measureRows(rows).get(jobId);
      if (gone) flyToBin([gone]);
    }
    setPage((current) =>
      current
        ? {
            ...current,
            // A dismissed posting leaves the list now, as it will on every later read.
            binCount:
              decision === "dismissed" ? current.binCount + 1 : current.binCount,
            items: current.items
              .filter((row) => row.id !== jobId || decision !== "dismissed")
              .map((row) =>
                row.id === jobId
                  ? {
                      ...row,
                      label,
                      labelStale: false,
                      decision,
                      isSaved: decision === "saved",
                    }
                  : row,
              ),
          }
        : current,
    );
  }

  /** Changes one row in place, so the list updates without a refetch. */
  function patchRow(jobId: number, patch: Partial<ReviewRow>) {
    setPage((current) =>
      current
        ? {
            ...current,
            items: current.items.map((row) =>
              row.id === jobId ? { ...row, ...patch } : row,
            ),
          }
        : current,
    );
  }

  /** Mirrors a decision on the row, the way `remember` mirrors a label. Null is no
   * decision at all: what an undo restores for a job that had none. */
  function noteDecision(jobId: number, decision: JobDecision | null) {
    patchRow(jobId, {
      decision,
      isSaved: decision === "saved",
      isApplied: decision === "applied",
    });
  }

  /** Applied is saved first, always; only then is the Notion question asked. */
  async function markApplied(row: ReviewRow) {
    setBusy(true);
    try {
      await saveDecision(row.id, "applied");
      noteDecision(row.id, "applied");
      setJustApplied((current) => new Set([...current, row.id]));
      setNotice("Applied. It stays in your list and is not scored again.");
      setApplying(row.id);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  /** Puts back the decision from before Applied; runs only after the user confirmed it. */
  async function confirmUndo(row: ReviewRow) {
    setUndoing(null);
    setBusy(true);
    try {
      const { decision } = await undoApplied(row.id);
      noteDecision(row.id, decision);
      // Stays listed for this visit, so the pane does not vanish under the notice.
      setJustApplied((current) => new Set([...current, row.id]));
      setApplying((current) => (current === row.id ? null : current));
      setNotice(`Applied undone.${notionKept(row.notionIndex)}`);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  async function decide(jobId: number, label: ReviewLabel, notice?: string) {
    setBusy(true);
    try {
      await saveReview(jobId, label);
      remember(jobId, label);
      setNotice(notice ?? NOTICE_FOR_LABEL[label]);
      setAskWhy(label === "not_relevant" ? jobId : null);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  async function giveReason(reason: DismissReason) {
    const jobId = askWhy;
    setAskWhy(null);
    if (jobId === null) return;
    try {
      await saveDecision(jobId, "dismissed", reason);
      setNotice("Thanks. Future matches will take that into account.");
    } catch (failure) {
      setError(message(failure));
    }
  }

  /** Applies what the assistant did to this page's own state. */
  function applyActions(actions: AssistantAction[]) {
    for (const action of actions) {
      if (action.kind === "set_view") {
        setIncludeBelow(action.includeBelow);
        setLimit(action.limit);
      } else if (action.kind === "jobs_changed") {
        // The chat already says what changed; the page only has to show it.
        // The rows are measured first: once the refetch lands, the ones the chat removed
        // are gone from the DOM, and only this snapshot says where they were.
        const before = measureRows(rows);
        void refetch()
          .then((ids) => {
            const kept = new Set(ids);
            flyToBin([...before.values()].filter((item) => !kept.has(item.row.id)));
          })
          .catch(() => undefined);
      }
    }
  }

  /** The placeholder for a dismissed row, in its old place. The row itself is already gone
   * from the list; this one only exists to be animated away. */
  const ghostRow = (ghost: Leaving) => (
    <li
      aria-hidden="true"
      className="job-row row-leaving"
      data-testid="leaving-row"
      key={`leaving-${ghost.row.id}`}
      style={
        {
          "--bin-dx": `${ghost.dx}px`,
          "--bin-dy": `${ghost.dy}px`,
        } as CSSProperties
      }
    >
      <span className="job-row-main">
        <span className="job-score" />
        <span className="job-body">
          <span className="job-title">{ghost.row.title}</span>
          <span className="job-sub">{ghost.row.company}</span>
        </span>
      </span>
    </li>
  );

  // Applied postings stay out of the way until the Applied pill asks for them. Each pill
  // narrows the list to its own postings; with both on, a posting matching either stays.
  const shown = rows.filter((row) => {
    if (justApplied.has(row.id)) return true;
    if (!showApplied && !onlyInterested) return !row.isApplied;
    return (
      (showApplied && row.isApplied) ||
      (onlyInterested && row.isSaved && !row.isApplied)
    );
  });
  // The filter's options and counts come from what is listed before the location cut,
  // so picking a city never makes the other cities vanish from the menu.
  const cityCounts = new Map<string, number>();
  for (const row of shown) {
    for (const city of citiesOf(row.location)) {
      cityCounts.set(city, (cityCounts.get(city) ?? 0) + 1);
    }
  }
  const locationOptions = [...cityCounts]
    .map(([city, count]) => ({ city, count }))
    .sort((a, b) => b.count - a.count || a.city.localeCompare(b.city));
  const inLocations = selectedLocations.length
    ? shown.filter((row) =>
        citiesOf(row.location).some((city) => selectedLocations.includes(city)),
      )
    : shown;
  // Matches a company name anywhere in it, ignoring case, so "hels" finds Helsing.
  const wanted = companyQuery.trim().toLowerCase();
  const visible = wanted
    ? inLocations.filter((row) => row.company.toLowerCase().includes(wanted))
    : inLocations;
  const selected =
    visible.find((row) => row.id === selectedId) ?? visible[0] ?? null;
  const openIndex = visible.findIndex((row) => row.id === openJob);
  const filtersChanged =
    threshold !== MIN_THRESHOLD ||
    includeBelow ||
    showApplied ||
    onlyInterested ||
    wanted !== "" ||
    selectedLocations.length > 0;

  function resetFilters() {
    setLoading(true);
    setThreshold(MIN_THRESHOLD);
    setIncludeBelow(false);
    setShowApplied(false);
    setOnlyInterested(false);
    setSelectedLocations([]);
    setCompanyQuery("");
    setJustApplied(new Set());
  }

  /** A plain click selects; ctrl/cmd-click goes straight to the posting. */
  function pick(event: MouseEvent, row: ReviewRow) {
    if (event.ctrlKey || event.metaKey) {
      window.open(row.url, "_blank", "noopener,noreferrer");
      return;
    }
    setSelectedId(row.id);
  }

  return (
    <>
      <div className="review-title">
        <h1>Recommendations</h1>
        <div className="review-actions">
          <Link className="soft-link" href="/resume">
            Update CV
          </Link>
          <Link className="soft-link" href="/preferences">
            Edit preferences
          </Link>
          <Link
            className={pulsing ? "bin-link bin-pulse" : "bin-link"}
            href="/recycle-bin"
            ref={binRef}
          >
            <span aria-hidden="true">🗑️</span>
            Recycle bin ({binCount})
          </Link>
          <RefreshJobs
            knownIds={rows.map((row) => row.id)}
            onRefetch={refetch}
          />
        </div>
      </div>
      <div className="review-controls">
        <input
          aria-label="Search companies"
          className="company-search"
          onChange={(event) => setCompanyQuery(event.target.value)}
          placeholder="Search company"
          type="search"
          value={companyQuery}
        />
        <LocationFilter
          options={locationOptions}
          selected={selectedLocations}
          onChange={setSelectedLocations}
        />
        <label className="threshold">
          SCORE ≥
          <input
            aria-label="Show scores at or above"
            max={MAX_THRESHOLD}
            min={MIN_THRESHOLD}
            step={5}
            type="range"
            value={threshold}
            onChange={(event) => {
              setLoading(true);
              setThreshold(Number(event.target.value));
            }}
          />
          <output>{threshold}</output>
        </label>
        <label className="threshold">
          SHOW
          <select
            aria-label="Show up to"
            value={limit}
            onChange={(event) => {
              setLoading(true);
              setLimit(Number(event.target.value));
            }}
          >
            {[...new Set([...LIMIT_STEPS, limit])]
              .sort((a, b) => a - b)
              .map((step) => (
                <option key={step} value={step}>
                  {step}
                </option>
              ))}
          </select>
        </label>
        <div className="filter-chips" role="group" aria-label="Filter">
          <button
            aria-pressed={includeBelow}
            className="chip"
            type="button"
            onClick={() => {
              setLoading(true);
              setIncludeBelow(!includeBelow);
            }}
          >
            Show lower matches
          </button>
          <button
            aria-pressed={showApplied}
            className="chip"
            type="button"
            onClick={() => setShowApplied(!showApplied)}
          >
            Applied
          </button>
          <button
            aria-pressed={onlyInterested}
            className="chip"
            type="button"
            onClick={() => setOnlyInterested(!onlyInterested)}
          >
            Interested
          </button>
        </div>
      </div>

      <p aria-live="polite" className="page-notice">
        {notice}
      </p>
      {askWhy !== null ? (
        <div className="reason-row" role="group" aria-label="Why was it dismissed?">
          <span>Dismissed. Why? (optional)</span>
          {DISMISS_REASONS.map((reason) => (
            <button
              className="chip"
              key={reason.value}
              type="button"
              onClick={() => void giveReason(reason.value)}
            >
              {reason.label}
            </button>
          ))}
          <button className="chip" type="button" onClick={() => setAskWhy(null)}>
            Skip
          </button>
        </div>
      ) : null}
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="split-view">
        <section className="review-list" aria-labelledby="review-heading">
          <h2 className="visually-hidden" id="review-heading">
            Ranked postings
          </h2>
          <div className="result-meta">
            <span className="tally">
              {visible.length} {visible.length === 1 ? "RESULT" : "RESULTS"}
            </span>
            {filtersChanged ? (
              <button
                className="reset-filters"
                type="button"
                onClick={resetFilters}
              >
                Reset filters
              </button>
            ) : null}
          </div>
          <ul className="job-rows">
            {visible.map((row, index) => (
              <Fragment key={row.id}>
                {leaving
                  .filter((ghost) => ghost.index === index)
                  .map(ghostRow)}
              <li
                data-job-id={row.id}
                className={
                  [
                    "job-row",
                    row.id === selected?.id ? "selected" : "",
                    row.belowThreshold ? "below-threshold" : "",
                    row.locationMismatch ? "location-mismatch" : "",
                    row.isSaved ? "saved-row" : "",
                    row.isApplied ? "applied-row" : "",
                  ]
                    .filter(Boolean)
                    .join(" ")
                }
              >
                <button
                  aria-current={row.id === selected?.id ? "true" : undefined}
                  aria-label={`Select: ${row.title}`}
                  className="job-row-main"
                  type="button"
                  onClick={(event) => pick(event, row)}
                >
                  <span className="job-score" data-tier={tier(row.score)}>
                    {scoreLabel(row)}
                  </span>
                  <span className="job-body">
                    <span className="job-title">
                      {row.isSaved ? (
                        <span aria-label="Saved" className="pin" role="img">
                          📌
                        </span>
                      ) : null}
                      <span className="job-title-text">{row.title}</span>
                      {row.isNew || freshIds.has(row.id) ? (
                        <span className="new-badge">NEW</span>
                      ) : null}
                      {row.isSaved ? (
                        <span className="status-tag status-interested">
                          INTERESTED
                        </span>
                      ) : null}
                      {row.isApplied ? (
                        <span className="applied-badge">APPLIED</span>
                      ) : null}
                    </span>
                    <span className="job-sub">
                      {row.company} · {age(row.firstSeenAt)}
                      {mayBeClosed(row.lastSeenAt, inactiveAfterDays)
                        ? " · MAY BE CLOSED"
                        : ""}
                      {row.belowThreshold ? (
                        <span className="lower-match">Lower match</span>
                      ) : null}
                      {row.locationMismatch ? (
                        <span className="mismatch-tag">Location mismatch</span>
                      ) : null}
                    </span>
                  </span>
                  <span className="job-location">{row.location || "—"}</span>
                </button>
              </li>
              </Fragment>
            ))}
            {leaving
              .filter((ghost) => ghost.index >= visible.length)
              .map(ghostRow)}
          </ul>
          {!loading && !visible.length ? (
            <div className="list-empty">
              {rows.length ? (
                <>
                  <p className="list-empty-title">
                    No matches for these filters
                  </p>
                  <button
                    className="reset-filters"
                    type="button"
                    onClick={resetFilters}
                  >
                    Reset filters
                  </button>
                </>
              ) : (
                <p>
                  Nothing to review yet. Run the pipeline from Settings, or
                  lower the threshold.
                </p>
              )}
            </div>
          ) : null}
          {canShowMore ? (
            <p className="meta">
              Showing the top {limit}.{" "}
              {limit < DEFAULT_LIMIT
                ? "Raise “Show” to see more."
                : "Raise the threshold to narrow the list."}
            </p>
          ) : null}
        </section>

        <div className="detail-pane">
          {selected ? (
            <ReviewDetail
              asking={applying === selected.id}
              busy={busy}
              fresh={selected.isNew || freshIds.has(selected.id)}
              inactiveAfterDays={inactiveAfterDays}
              key={selected.id}
              row={selected}
              undoing={undoing === selected.id}
              onApplied={() =>
                selected.isApplied
                  ? setUndoing(selected.id)
                  : void markApplied(selected)
              }
              onCancelUndo={() => setUndoing(null)}
              onCloseNotion={() => setApplying(null)}
              onConfirmUndo={() => void confirmUndo(selected)}
              onInterested={() =>
                // A toggle: pressed again, it goes back to a neutral "maybe".
                selected.label === "good_fit" || selected.isSaved
                  ? void decide(
                      selected.id,
                      "maybe",
                      "No longer interested. It stays in the list.",
                    )
                  : void decide(selected.id, "good_fit")
              }
              onNotForMe={() => void decide(selected.id, "not_relevant")}
              onNotionAdded={(id, index) => patchRow(id, { notionIndex: index })}
              onOpenDetails={() => setOpenJob(selected.id)}
            />
          ) : null}
        </div>
      </div>

      {openJob !== null && openIndex >= 0 ? (
        <div
          aria-label="Full details"
          aria-modal="true"
          className="card-overlay"
          role="dialog"
          onKeyDown={(event) => {
            if (event.key === "Escape") setOpenJob(null);
          }}
          // A press that starts and ends on the dimmed area, outside the card, goes back
          // to the list; a drag that began inside the card and ended outside does not.
          onMouseDown={(event) => {
            pressedBackdrop.current = event.target === event.currentTarget;
          }}
          onClick={(event) => {
            if (pressedBackdrop.current && event.target === event.currentTarget) {
              setOpenJob(null);
            }
            pressedBackdrop.current = false;
          }}
        >
          <div className="card-overlay-body">
            <ReviewCard
              inactiveAfterDays={inactiveAfterDays}
              jobId={openJob}
              key={openJob}
              onLabelled={(id, label) => {
                remember(id, label);
                setNotice(NOTICE_FOR_LABEL[label]);
                setAskWhy(label === "not_relevant" ? id : null);
              }}
              onDecided={noteDecision}
              onNotionAdded={(id, index) => patchRow(id, { notionIndex: index })}
              index={openIndex}
              onGo={(next) => {
                scrollBeforeGo.current = window.scrollY;
                const target = visible[next]?.id ?? openJob;
                setOpenJob(target);
                setSelectedId(target);
              }}
              onLoaded={() => {
                if (scrollBeforeGo.current === null) return;
                window.scrollTo(0, scrollBeforeGo.current);
                scrollBeforeGo.current = null;
              }}
              total={visible.length}
            />
          </div>
        </div>
      ) : null}

      <AssistantChat
        knownIds={rows.map((row) => row.id)}
        view={{ limit, includeBelow }}
        onActions={applyActions}
        onRefetch={refetch}
      />
    </>
  );
}
