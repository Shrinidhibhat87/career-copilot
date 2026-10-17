"use client";

import { useEffect, useState } from "react";

import { CompanyName } from "@/components/company-name";
import {
  NotionPrompt,
  UndoAppliedConfirm,
} from "@/components/notion-prompt";
import { ScoreDonut } from "@/components/score-donut";
import { age, mayBeClosed } from "@/lib/freshness";
import { getJob, type ReviewRow, type ScoreParts } from "@/lib/api";

/** The right-hand pane of the split view: one posting, its score breakdown, why it
 * matches, and the verdict buttons. The list rows carry no breakdown, so it is fetched
 * for the selected posting alone. */
export function ReviewDetail({
  row,
  fresh,
  busy,
  inactiveAfterDays,
  asking,
  undoing,
  onInterested,
  onNotForMe,
  onApplied,
  onOpenDetails,
  onCancelUndo,
  onConfirmUndo,
  onCloseNotion,
  onNotionAdded,
}: {
  row: ReviewRow;
  fresh: boolean;
  busy: boolean;
  inactiveAfterDays: number;
  /** The Notion question is open beside Applied. */
  asking: boolean;
  /** The undo-applied confirmation is open beside Applied. */
  undoing: boolean;
  onInterested: () => void;
  onNotForMe: () => void;
  onApplied: () => void;
  onOpenDetails: () => void;
  onCancelUndo: () => void;
  onConfirmUndo: () => void;
  onCloseNotion: () => void;
  onNotionAdded: (jobId: number, index: number) => void;
}) {
  const [loaded, setLoaded] = useState<{
    id: number;
    parts: ScoreParts | null;
  } | null>(null);

  useEffect(() => {
    let disposed = false;
    void getJob(row.id)
      .then((card) => {
        if (!disposed) setLoaded({ id: row.id, parts: card.parts });
      })
      .catch(() => {
        // The breakdown is a nicety: the plain number stands in for it.
        if (!disposed) setLoaded({ id: row.id, parts: null });
      });
    return () => {
      disposed = true;
    };
  }, [row.id]);

  const parts = loaded?.id === row.id ? loaded.parts : null;
  const interested = row.label === "good_fit" || row.isSaved;
  const closed = mayBeClosed(row.lastSeenAt, inactiveAfterDays);

  return (
    <aside aria-label="Selected posting" className="review-detail">
      <div className="detail-meta">
        <span>
          <span className="source-name">{row.source}</span> ·{" "}
          {age(row.firstSeenAt)} · last seen {age(row.lastSeenAt)}
          {closed ? " · MAY BE CLOSED" : ""}
        </span>
        {fresh ? <span className="new-badge">NEW</span> : null}
      </div>
      <h2 className="detail-title">
        <a href={row.url} rel="noreferrer" target="_blank">
          {row.title}
          <span aria-hidden="true"> ↗</span>
        </a>
      </h2>
      <p className="detail-company">
        <CompanyName name={row.company} url={row.companyUrl} />
        {row.location ? <> — {row.location}</> : null}
      </p>

      <div className="detail-score" data-slot="score-donut">
        {parts ? (
          <ScoreDonut parts={parts} />
        ) : (
          <strong className="detail-score-number">{row.score ?? "—"}</strong>
        )}
      </div>

      <h3 className="detail-label">WHY IT MATCHES</h3>
      <p className="detail-why">{row.why || "No reason recorded."}</p>

      <div className="detail-actions">
        <button
          aria-label={`Good fit: ${row.title}`}
          aria-pressed={interested}
          className="detail-interested"
          disabled={busy}
          type="button"
          onClick={onInterested}
        >
          ✓ Interested
        </button>
        <button
          aria-label={`Not relevant: ${row.title}`}
          className="detail-reject"
          disabled={busy}
          type="button"
          onClick={onNotForMe}
        >
          ✕ Not for me
        </button>
        <div className="detail-applied">
          <button
            aria-label={`Applied: ${row.title}`}
            aria-pressed={row.isApplied}
            className="detail-applied-button"
            disabled={busy}
            type="button"
            onClick={onApplied}
          >
            {row.isApplied ? "✓ Applied" : "Mark applied"}
          </button>
          {undoing ? (
            <UndoAppliedConfirm
              notionIndex={row.notionIndex}
              title={row.title}
              onCancel={onCancelUndo}
              onConfirm={onConfirmUndo}
            />
          ) : null}
          {asking ? (
            <NotionPrompt
              jobId={row.id}
              title={row.title}
              onAdded={onNotionAdded}
              onClose={onCloseNotion}
            />
          ) : null}
        </div>
        <button
          aria-label={`Details: ${row.title}`}
          className="detail-details"
          type="button"
          onClick={onOpenDetails}
        >
          Full details
        </button>
      </div>
    </aside>
  );
}
