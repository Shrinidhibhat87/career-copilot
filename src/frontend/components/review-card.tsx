"use client";

import { useEffect, useState } from "react";

import { CompanyName } from "@/components/company-name";
import {
  notionDone,
  NotionPrompt,
  UndoAppliedConfirm,
} from "@/components/notion-prompt";
import { ScoreDonut } from "@/components/score-donut";
import { age, mayBeClosed } from "@/lib/freshness";
import { cardNext, cardPrev } from "@/lib/pager";

import {
  addToNotion,
  getJob,
  getJobOutline,
  saveCorrection,
  saveDecision,
  saveReview,
  undoApplied,
  type JobCard,
  type JobDecision,
  type JobOutline,
  type ReviewLabel,
} from "@/lib/api";

const LABELS: { id: ReviewLabel; text: string; className: string }[] = [
  { id: "good_fit", text: "✓ Good fit", className: "verdict-good" },
  { id: "not_relevant", text: "✕ Not relevant", className: "verdict-bad" },
  { id: "maybe", text: "Maybe", className: "verdict-maybe" },
];

/** What the card says right beside the buttons once a verdict is saved. */
const CONFIRM: Record<ReviewLabel, string> = {
  good_fit: "Saved",
  not_relevant: "Marked not relevant",
  maybe: "Marked maybe",
};

const message = (error: unknown) =>
  error instanceof Error ? error.message : "The request failed.";

const NOT_STATED = "Not stated in the posting";

/** What the role involves and asks for, in the posting's own words. A posting opens with
 * the company's pitch, so the first lines say nothing about the role; a model picks the two
 * parts out and the server checks every sentence against the text. */
function PostingOutline({ jobId }: { jobId: number }) {
  // null while it is being read; "failed" when the request itself did not get through.
  const [outline, setOutline] = useState<JobOutline | "failed" | null>(null);

  useEffect(() => {
    let disposed = false;
    void getJobOutline(jobId)
      .then((result) => {
        if (!disposed) setOutline(result);
      })
      .catch(() => {
        if (!disposed) setOutline("failed");
      });
    return () => {
      disposed = true;
    };
  }, [jobId]);

  if (outline === null) {
    return (
      <p aria-busy="true" className="meta card-outline-note" role="status">
        Reading the posting…
      </p>
    );
  }
  if (outline === "failed" || outline.status !== "ok") {
    return (
      <p className="card-outline-note" role="alert">
        The summary is not working right now. Open the original posting to read the
        description.
      </p>
    );
  }
  const sections: [string, string[]][] = [
    ["Responsibilities", outline.responsibilities],
    ["Requirements", outline.requirements],
  ];
  return (
    <div className="card-outline">
      {sections.map(([heading, items]) => (
        <section key={heading}>
          <h3>{heading}</h3>
          {items.length ? (
            <ul>
              {items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <p className="meta">{NOT_STATED}</p>
          )}
        </section>
      ))}
    </div>
  );
}

export function ReviewCard({
  inactiveAfterDays,
  jobId,
  index,
  total,
  onGo,
  onLoaded,
  onLabelled,
  onDecided,
  onNotionAdded,
}: {
  inactiveAfterDays: number;
  jobId: number;
  /** Zero-based place of this card among the `total` shown. */
  index: number;
  total: number;
  /** Asks the parent to show the card at this index. */
  onGo: (index: number) => void;
  /** The posting has been fetched and is on screen. */
  onLoaded?: () => void;
  onLabelled: (jobId: number, label: ReviewLabel) => void;
  /** Applied and its undo (null: the job had no decision before Applied). */
  onDecided: (jobId: number, decision: JobDecision | null) => void;
  /** A row was added to Notion from this card. */
  onNotionAdded?: (jobId: number, index: number) => void;
}) {
  const [card, setCard] = useState<JobCard | null>(null);
  const [error, setError] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  // Claims whose evidence the user flagged; each can be flagged once.
  const [noted, setNoted] = useState<ReadonlySet<string>>(new Set());
  // The card's own "Add to Notion" outcome: what to say, or why it failed.
  const [notionText, setNotionText] = useState("");
  const [notionError, setNotionError] = useState("");
  const [confirmingUndo, setConfirmingUndo] = useState(false);
  // The Notion question, asked beside the Applied button right after it is saved.
  const [asking, setAsking] = useState(false);

  // The parent keys this component by job id, so moving to the next posting remounts it
  // rather than resetting state from inside the effect.
  useEffect(() => {
    let disposed = false;
    void getJob(jobId)
      .then((result) => {
        if (!disposed) {
          setCard(result);
          setError("");
        }
      })
      .catch((failure: unknown) => {
        if (!disposed) setError(message(failure));
      });
    return () => {
      disposed = true;
    };
  }, [jobId]);

  useEffect(() => {
    if (card) onLoaded?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per loaded posting
  }, [card]);

  async function label(value: ReviewLabel) {
    setBusy(true);
    try {
      await saveReview(jobId, value);
      onLabelled(jobId, value);
      setCard((current) =>
        current ? { ...current, label: value, labelStale: false } : current,
      );
      setConfirmation(CONFIRM[value]);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  function noteDecision(decision: JobDecision | null) {
    setCard((current) =>
      current
        ? {
            ...current,
            decision,
            isSaved: decision === "saved",
            isApplied: decision === "applied",
          }
        : current,
    );
    onDecided(jobId, decision);
  }

  /** Applied is saved first, always; only then is the Notion question asked. */
  async function markApplied() {
    setBusy(true);
    try {
      await saveDecision(jobId, "applied");
      noteDecision("applied");
      setConfirmation("Marked applied");
      setAsking(true);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  /** Puts back the decision from before Applied. Both are appended, never edits. */
  async function confirmUndo() {
    setConfirmingUndo(false);
    setAsking(false);
    setBusy(true);
    try {
      const { decision } = await undoApplied(jobId);
      noteDecision(decision);
      setConfirmation("Applied undone");
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  async function sendToNotion() {
    setBusy(true);
    setNotionError("");
    try {
      const result = await addToNotion(jobId);
      setCard((current) =>
        current ? { ...current, notionIndex: result.index } : current,
      );
      setNotionText(notionDone(result));
      onNotionAdded?.(jobId, result.index);
    } catch (failure) {
      setNotionError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  async function dispute(target: string) {
    setBusy(true);
    try {
      await saveCorrection(jobId, "evidence", target);
      setNoted((current) => new Set([...current, target]));
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  if (error) {
    return (
      <p className="form-error" role="alert">
        {error}
      </p>
    );
  }
  if (!card) return <p role="status">Loading the posting…</p>;

  const claims = card.reasons.length ? card.reasons : card.triageReasons;
  const notionIndex = card.notionIndex;
  return (
    <>
      <div className="card-bar">
        <div className="card-pager">
          <button
            aria-label="Previous posting"
            className="ghost-button"
            disabled={total <= 1}
            type="button"
            onClick={() => onGo(cardPrev(index, total))}
          >
            ←
          </button>
          <span className="meta">
            {index + 1} / {total}
          </span>
          <button
            aria-label="Next posting"
            className="ghost-button"
            disabled={total <= 1}
            type="button"
            onClick={() => onGo(cardNext(index, total))}
          >
            →
          </button>
        </div>
      </div>
      <article className="review-card">
        <section className="card-posting">
          <div className="badge-row">
            {card.isSaved ? (
              <span className="provenance provenance-pass">SAVED</span>
            ) : null}
            {card.isApplied ? (
              <span className="provenance applied-badge">APPLIED</span>
            ) : null}
            {card.labelStale ? (
              <span className="provenance provenance-manual_review">
                POSTING CHANGED SINCE
              </span>
            ) : null}
          </div>
          <h2>{card.title}</h2>
          <p className="card-company">
            <CompanyName name={card.company} url={card.companyUrl} />
          </p>
          <p className="meta">
            {card.location || "location not stated"} · {card.source}
          </p>
          <PostingOutline jobId={jobId} />
          <a href={card.url} rel="noreferrer" target="_blank">
            Open original posting ↗
          </a>
          {/* The board drops a filled role long before we retire it, so a link this old
              is likely to land on its "no longer available" page. */}
          <p className="meta">
            Last seen on the board {age(card.lastSeenAt)}
            {mayBeClosed(card.lastSeenAt, inactiveAfterDays)
              ? " · MAY BE CLOSED"
              : ""}
          </p>
        </section>

        <section className="card-verdict">
          <div className="card-score" data-slot="score-donut">
            {card.parts ? (
              <ScoreDonut parts={card.parts} />
            ) : (
              <>
                <strong>{card.score ?? "—"}</strong>
                {/* A match score from before breakdowns existed: the total only. */}
                {card.scoreStage === "match" && card.rubricVersion === 1 ? (
                  <span className="meta">Scored before breakdowns existed</span>
                ) : null}
              </>
            )}
          </div>

          <h3>Why it may fit</h3>
          {claims.length ? (
            <ul className="claim-list">
              {claims.map((claim, claimIndex) => (
                <li key={claim}>
                  <p>{claim}</p>
                  {card.evidence[claimIndex] ? (
                    <details className="evidence">
                      <summary>From your CV</summary>
                      <q>{card.evidence[claimIndex]}</q>
                      <button
                        aria-busy={busy}
                        className="ghost-button"
                        disabled={busy || noted.has(claim)}
                        type="button"
                        onClick={() => void dispute(claim)}
                      >
                        {noted.has(claim) ? "Noted" : "Evidence is wrong"}
                      </button>
                    </details>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p>No match claims — decided by rules.</p>
          )}

          <h3>Gaps</h3>
          {card.gaps.length ? (
            <ul className="gap-list">
              {card.gaps.map((gap) => (
                <li key={gap}>{gap}</li>
              ))}
            </ul>
          ) : (
            <p>None recorded.</p>
          )}

          <div className="verdict-actions">
            {LABELS.map((choice) => (
              <button
                aria-busy={busy}
                aria-pressed={card.label === choice.id}
                className={choice.className}
                disabled={busy}
                key={choice.id}
                type="button"
                onClick={() => void label(choice.id)}
              >
                {choice.text}
              </button>
            ))}
          </div>
          <div className="verdict-actions">
            {card.isApplied ? (
              <button
                aria-busy={busy}
                className="ghost-button"
                disabled={busy}
                type="button"
                onClick={() => setConfirmingUndo(true)}
              >
                Undo applied
              </button>
            ) : (
              <button
                aria-busy={busy}
                className="applied-action"
                disabled={busy}
                type="button"
                onClick={() => void markApplied()}
              >
                Applied
              </button>
            )}
            {confirmingUndo ? (
              <UndoAppliedConfirm
                notionIndex={notionIndex}
                title={card.title}
                onCancel={() => setConfirmingUndo(false)}
                onConfirm={() => void confirmUndo()}
              />
            ) : null}
            {asking ? (
              <NotionPrompt
                jobId={jobId}
                title={card.title}
                onAdded={(id, index) => {
                  setCard((current) =>
                    current ? { ...current, notionIndex: index } : current,
                  );
                  onNotionAdded?.(id, index);
                }}
                onClose={() => setAsking(false)}
              />
            ) : null}
          </div>
          <p aria-live="polite" className="confirm">
            {confirmation}
          </p>
          {card.isApplied ? (
            <div className="card-notion">
              {notionIndex !== null ? (
                <p className="meta">
                  {notionText || `In Notion as row ${notionIndex}.`}
                </p>
              ) : (
                <>
                  <button
                    aria-busy={busy}
                    className="ghost-button"
                    disabled={busy}
                    type="button"
                    onClick={() => void sendToNotion()}
                  >
                    Add to Notion
                  </button>
                  {notionError ? (
                    <p className="form-error" role="alert">
                      {notionError}
                    </p>
                  ) : null}
                </>
              )}
            </div>
          ) : null}
        </section>
      </article>
    </>
  );
}
