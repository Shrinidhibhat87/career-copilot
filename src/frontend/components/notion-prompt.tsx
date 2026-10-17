"use client";

import { useState } from "react";

import { addToNotion, type NotionAdd } from "@/lib/api";

const QUESTION = "Add this to your Notion Applications database?";

const message = (error: unknown) =>
  error instanceof Error ? error.message : "The request failed.";

/** What the screen says once the job is in Notion, however it got there. */
export const notionDone = (result: NotionAdd) =>
  result.status === "already"
    ? `Already in Notion (row ${result.index}).`
    : `Added to Notion as row ${result.index}.`;

type State =
  | { phase: "ask" }
  | { phase: "busy" }
  | { phase: "done"; text: string }
  | { phase: "failed"; reason: string };

/** Asked once, right after Applied is saved, in a popover beside the Applied button. No
 * sends nothing; Yes sends one request, and a failure keeps the job Applied and offers
 * "Add to Notion" to try again. */
export function NotionPrompt({
  jobId,
  title,
  onAdded,
  onClose,
}: {
  jobId: number;
  title: string;
  onAdded: (jobId: number, index: number) => void;
  onClose: () => void;
}) {
  const [state, setState] = useState<State>({ phase: "ask" });

  async function add() {
    setState({ phase: "busy" });
    try {
      const result = await addToNotion(jobId);
      onAdded(jobId, result.index);
      setState({ phase: "done", text: notionDone(result) });
    } catch (failure) {
      setState({ phase: "failed", reason: message(failure) });
    }
  }

  return (
    <div
      aria-labelledby="notion-prompt-heading"
      aria-modal="true"
      className="notion-prompt"
      role="dialog"
    >
      <p className="meta">Marked applied: {title}</p>
      <h2 id="notion-prompt-heading">{QUESTION}</h2>
      {state.phase === "ask" ? (
        <div className="notion-prompt-actions">
          <button className="primary-action" type="button" onClick={() => void add()}>
            Yes
          </button>
          <button className="ghost-button" type="button" onClick={onClose}>
            No
          </button>
        </div>
      ) : null}
      {state.phase === "busy" ? <p role="status">Adding to Notion…</p> : null}
      {state.phase === "done" ? (
        <>
          <p role="status">{state.text}</p>
          <button className="ghost-button" type="button" onClick={onClose}>
            Close
          </button>
        </>
      ) : null}
      {state.phase === "failed" ? (
        <>
          <p className="form-error" role="alert">
            {state.reason}
          </p>
          <div className="notion-prompt-actions">
            <button className="primary-action" type="button" onClick={() => void add()}>
              Add to Notion
            </button>
            <button className="ghost-button" type="button" onClick={onClose}>
              Close
            </button>
          </div>
        </>
      ) : null}
    </div>
  );
}

/** What undoing says about Notion: nothing, unless the job really has a row there. */
export const notionKept = (notionIndex: number | null) =>
  notionIndex === null
    ? ""
    : ` Its Notion row ${notionIndex} stays; delete it in Notion if you want it gone.`;

/** Unchecking Applied is a second, deliberate step. It puts back the job's state from
 * before Applied; a row already in Notion stays there. */
export function UndoAppliedConfirm({
  title,
  notionIndex,
  onConfirm,
  onCancel,
}: {
  title: string;
  notionIndex: number | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      aria-describedby="undo-applied-text"
      aria-labelledby="undo-applied-heading"
      aria-modal="true"
      className="notion-prompt"
      role="alertdialog"
    >
      <h2 id="undo-applied-heading">Undo applied?</h2>
      <p className="meta" id="undo-applied-text">
        {title} goes back to how it was before you marked it applied.
        {notionKept(notionIndex)}
      </p>
      <div className="notion-prompt-actions">
        <button className="primary-action" type="button" onClick={onConfirm}>
          Undo applied
        </button>
        <button className="ghost-button" type="button" onClick={onCancel}>
          Keep applied
        </button>
      </div>
    </div>
  );
}
