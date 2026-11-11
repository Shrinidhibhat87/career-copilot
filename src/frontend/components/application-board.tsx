"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";

import {
  PIPELINE_EXPORT_URL,
  STAGE_LABELS,
  type Pipeline,
  type PipelineCard,
  type Stage,
  type TimelineEntry,
  getPipeline,
  getTimeline,
  hideCard,
  saveCardNote,
  saveStaleDays,
  setStage,
  unhideCard,
} from "@/lib/api";

const FLASH_MS = 4000;
const STAGES: Stage[] = ["applied", "interviewing", "offer", "rejected", "withdrawn"];
const CLOSED: Stage[] = ["offer", "rejected", "withdrawn"];
type Column = "applied" | "interviewing" | "closed";

const day = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

/** Applied jobs as three columns. A card moves by drag or by its menu; Closed leads with
 * offers and folds rejected and withdrawn away until asked. Hide parks a card in a list
 * that Restore empties, so nothing is ever deleted here. */
export function ApplicationBoard() {
  const [board, setBoard] = useState<Pipeline | null>(null);
  const [error, setError] = useState("");
  const [showFolded, setShowFolded] = useState(false);
  const [showHidden, setShowHidden] = useState(false);
  const [closing, setClosing] = useState<number | null>(null);
  const [days, setDays] = useState("14");
  const [saved, setSaved] = useState(false);
  const flash = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const show = useCallback((next: Pipeline) => {
    setBoard(next);
    setDays(String(next.staleDays));
  }, []);

  useEffect(() => {
    getPipeline()
      .then(show)
      .catch((reason: Error) => setError(reason.message));
    return () => clearTimeout(flash.current);
  }, [show]);

  async function act(action: () => Promise<unknown>) {
    setError("");
    try {
      await action();
      setClosing(null);
      show(await getPipeline());
    } catch (reason) {
      setError((reason as Error).message);
    }
  }

  const move = (card: PipelineCard, stage: Stage) => act(() => setStage(card.jobId, stage));

  async function saveDays() {
    await act(() => saveStaleDays(Number(days)));
    setSaved(true);
    clearTimeout(flash.current);
    flash.current = setTimeout(() => setSaved(false), FLASH_MS);
  }

  const everyone = board
    ? [...board.applied, ...board.interviewing, ...board.closed.offers, ...board.closed.folded]
    : [];

  function drop(event: DragEvent, column: Column) {
    event.preventDefault();
    const card = everyone.find((c) => String(c.jobId) === event.dataTransfer.getData("text/plain"));
    if (!card) return;
    const from: Column = CLOSED.includes(card.stage) ? "closed" : (card.stage as Column);
    if (from === column) return;
    if (column === "closed") setClosing(card.jobId);
    else void move(card, column);
  }

  const total = board ? everyone.length + board.hidden.length : 0;
  const shared = {
    closing,
    onClose: (card: PipelineCard) => setClosing(card.jobId),
    onCancel: () => setClosing(null),
    onMove: move,
    onHide: (card: PipelineCard) => act(() => hideCard(card.jobId)),
    onNote: (card: PipelineCard, note: string) => act(() => saveCardNote(card.jobId, note)),
  };

  return (
    <>
      <div className="review-title">
        <div className="review-heading">
          <h1>Applications</h1>
        </div>
        <div className="review-actions">
          <Link className="soft-link" href="/recommendations">
            Back to Recommendations
          </Link>
          <a className="ghost-action" download href={PIPELINE_EXPORT_URL}>
            Download Excel
          </a>
          {board && board.hidden.length > 0 ? (
            <button
              aria-expanded={showHidden}
              className="ghost-action"
              onClick={() => setShowHidden((open) => !open)}
              type="button"
            >
              Hidden ({board.hidden.length})
            </button>
          ) : null}
          <form
            className="app-stale"
            onSubmit={(event) => {
              event.preventDefault();
              void saveDays();
            }}
          >
            <label>
              Quiet after{" "}
              <input
                aria-label="Quiet after days"
                max={365}
                min={1}
                onChange={(event) => setDays(event.target.value)}
                type="number"
                value={days}
              />{" "}
              days
            </label>
            <button
              className="ghost-action"
              disabled={!board || days === String(board.staleDays) || Number(days) < 1}
              type="submit"
            >
              Save
            </button>
            {saved ? <span className="app-saved">Saved.</span> : null}
          </form>
        </div>
      </div>
      {error ? (
        <p className="bin-error" role="alert">
          {error}
        </p>
      ) : null}
      {board === null ? null : total === 0 ? (
        <p className="bin-empty">
          Nothing applied yet. Mark a job Applied on Recommendations and it shows up here.
        </p>
      ) : (
        <>
          {showHidden ? (
            <section aria-label="Hidden applications" className="app-hidden">
              <h2>Hidden</h2>
              <ul>
                {board.hidden.map((card) => (
                  <li key={card.jobId}>
                    <span>
                      {card.title} · {card.company} · {STAGE_LABELS[card.stage]}
                    </span>
                    <button
                      className="ghost-action"
                      onClick={() => void act(() => unhideCard(card.jobId))}
                      type="button"
                    >
                      Restore
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}
          <div className="app-board">
            <BoardColumn
              cards={board.applied}
              column="applied"
              onDrop={drop}
              shared={shared}
              title="Applied"
            />
            <BoardColumn
              cards={board.interviewing}
              column="interviewing"
              onDrop={drop}
              shared={shared}
              title="Interviewing"
            />
            <BoardColumn
              cards={[...board.closed.offers, ...(showFolded ? board.closed.folded : [])]}
              column="closed"
              count={board.closed.offers.length + board.closed.folded.length}
              footer={
                board.closed.folded.length > 0 ? (
                  <button
                    className="ghost-action"
                    onClick={() => setShowFolded((open) => !open)}
                    type="button"
                  >
                    {showFolded
                      ? "Hide rejected and withdrawn"
                      : `Show ${board.closed.folded.length} rejected or withdrawn`}
                  </button>
                ) : null
              }
              onDrop={drop}
              shared={shared}
              title="Closed"
            />
          </div>
        </>
      )}
    </>
  );
}

type Shared = {
  closing: number | null;
  onClose: (card: PipelineCard) => void;
  onCancel: () => void;
  onMove: (card: PipelineCard, stage: Stage) => Promise<void>;
  onHide: (card: PipelineCard) => Promise<void>;
  onNote: (card: PipelineCard, note: string) => Promise<void>;
};

function BoardColumn({
  cards,
  column,
  count,
  footer,
  onDrop,
  shared,
  title,
}: {
  cards: PipelineCard[];
  column: Column;
  count?: number;
  footer?: React.ReactNode;
  onDrop: (event: DragEvent, column: Column) => void;
  shared: Shared;
  title: string;
}) {
  return (
    <section
      aria-label={title}
      className="app-column"
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => onDrop(event, column)}
    >
      <h2>
        {title} <span className="app-count">{count ?? cards.length}</span>
      </h2>
      <ul>
        {cards.map((card) => (
          <li key={card.jobId}>
            <AppCard card={card} shared={shared} />
          </li>
        ))}
      </ul>
      {footer}
    </section>
  );
}

function AppCard({ card, shared }: { card: PipelineCard; shared: Shared }) {
  const [open, setOpen] = useState(false);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [note, setNote] = useState(card.note);
  const closed = CLOSED.includes(card.stage);

  useEffect(() => {
    if (open) void getTimeline(card.jobId).then(setTimeline).catch(() => setTimeline([]));
  }, [open, card.jobId, card.stage]);

  return (
    <article
      className="app-card"
      data-stage={card.stage}
      draggable
      onDragStart={(event) => event.dataTransfer.setData("text/plain", String(card.jobId))}
    >
      <p className="app-title">
        {card.url ? (
          <a href={card.url} rel="noreferrer" target="_blank">
            {card.title}
          </a>
        ) : (
          card.title
        )}
      </p>
      <p className="app-meta">
        {card.company}
        {card.location ? ` · ${card.location}` : ""}
      </p>
      <p className="app-meta">
        Applied {day(card.appliedOn)}
        {closed ? (
          <span className="app-tag" data-stage={card.stage}>
            {STAGE_LABELS[card.stage]}
          </span>
        ) : null}
        {card.stale ? <span className="app-stale-badge">Quiet for {card.daysQuiet} days</span> : null}
      </p>
      {card.notionError ? (
        <p className="app-notion-error" role="status">
          Notion was not updated: {card.notionError}
        </p>
      ) : null}
      {shared.closing === card.jobId ? (
        <div aria-label={`Close ${card.title} as`} className="app-close" role="group">
          <span>Close as</span>
          {CLOSED.map((stage) => (
            <button
              className="ghost-action"
              key={stage}
              onClick={() => void shared.onMove(card, stage)}
              type="button"
            >
              {STAGE_LABELS[stage]}
            </button>
          ))}
          <button className="ghost-action" onClick={shared.onCancel} type="button">
            Cancel
          </button>
        </div>
      ) : (
        <div className="app-actions">
          <select
            aria-label={`Move ${card.title}`}
            onChange={(event) => {
              const stage = event.target.value as Stage;
              event.target.value = "";
              if (stage) void shared.onMove(card, stage);
            }}
            value=""
          >
            <option value="">Move to…</option>
            {STAGES.filter((stage) => stage !== card.stage).map((stage) => (
              <option key={stage} value={stage}>
                {STAGE_LABELS[stage]}
              </option>
            ))}
          </select>
          <button
            aria-expanded={open}
            className="ghost-action"
            onClick={() => setOpen((value) => !value)}
            type="button"
          >
            Details
          </button>
          <button className="ghost-action" onClick={() => void shared.onHide(card)} type="button">
            Hide
          </button>
        </div>
      )}
      {open ? (
        <div className="app-details">
          <ol aria-label="Timeline">
            {timeline.map((entry, index) => (
              <li key={index}>
                {STAGE_LABELS[entry.stage]} · {day(entry.at)}
                {entry.note ? ` · ${entry.note}` : ""}
              </li>
            ))}
          </ol>
          <label>
            Note
            <textarea
              maxLength={2000}
              onChange={(event) => setNote(event.target.value)}
              rows={3}
              value={note}
            />
          </label>
          <button
            className="ghost-action"
            disabled={note === card.note}
            onClick={() => void shared.onNote(card, note)}
            type="button"
          >
            Save note
          </button>
        </div>
      ) : null}
    </article>
  );
}
