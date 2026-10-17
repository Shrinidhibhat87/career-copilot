"use client";

import { useEffect, useState } from "react";

import { Disclosure } from "@/components/disclosure";

import {
  addIntent,
  approveCondensed,
  condenseIntent,
  getIntent,
  removeIntent,
  ValidationError,
  type IntentEntry,
  type IntentLog as Log,
} from "@/lib/api";

const message = (failure: unknown) => {
  if (failure instanceof ValidationError) {
    return Object.values(failure.errors).join(" ") || failure.message;
  }
  return failure instanceof Error ? failure.message : "Something went wrong.";
};

/** The server counts words for the log as a whole; between reads the screen keeps the
 * figure in step by the same whitespace rule, so the counter moves as entries do. */
const wordsIn = (text: string) => text.trim().split(/\s+/).filter(Boolean).length;

const shortDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });

/** What the user is looking for, as a log of entries rather than one sentence to rewrite.
 *
 * Each entry is kept as written and read by the agent that ranks and curates postings;
 * none of it is matched against a posting as a rule. Entries save the moment they are
 * added, independently of the Save button that holds the rest of the form.
 *
 * A long log can be condensed: the model drafts one note, the user edits it, and approving
 * it archives the entries and keeps the note in their place.
 */
export function IntentLog() {
  const [entries, setEntries] = useState<IntentEntry[] | null>(null);
  const [activeWords, setActiveWords] = useState(0);
  const [condenseAt, setCondenseAt] = useState(0);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The condensed draft under review; null when none is open.
  const [condensed, setCondensed] = useState<string | null>(null);

  function show(log: Log) {
    setEntries(log.entries);
    setActiveWords(log.activeWords);
    setCondenseAt(log.condenseAt);
  }

  useEffect(() => {
    let live = true;
    void getIntent()
      .then((log) => {
        if (live) show(log);
      })
      .catch((failure: unknown) => {
        if (live) setError(message(failure));
      });
    return () => {
      live = false;
    };
  }, []);

  async function add() {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    try {
      const entry = await addIntent(text);
      setEntries((current) => [...(current ?? []), entry]);
      setActiveWords((words) => words + wordsIn(entry.text));
      setDraft("");
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  async function remove(entry: IntentEntry) {
    setBusy(true);
    setError(null);
    try {
      await removeIntent(entry.id);
      setEntries((current) => (current ?? []).filter((other) => other.id !== entry.id));
      setActiveWords((words) => Math.max(0, words - wordsIn(entry.text)));
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  async function condense() {
    setBusy(true);
    setError(null);
    try {
      setCondensed(await condenseIntent());
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (condensed === null || !condensed.trim()) return;
    setBusy(true);
    setError(null);
    try {
      show(await approveCondensed(condensed.trim()));
      setCondensed(null);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  // The server returns entries oldest first; sorting here keeps that true after an add.
  const ordered = [...(entries ?? [])].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  const longEnough = entries !== null && activeWords >= condenseAt && ordered.length > 0;
  // Below the threshold, two entries can still be folded together; the button is quieter.
  const canCondense = longEnough || ordered.length >= 2;

  return (
    <div>
      {ordered.length > 0 ? (
        // Collapsed by default: past entries are history the agent reads, not text the user
        // needs in front of them while writing the next one.
        <Disclosure level="group" title={`Past entries (${ordered.length})`}>
          <ul className="proposal-list">
            {ordered.map((entry) => (
              <li className="proposal" key={entry.id}>
                <div>
                  <p className="proposal-summary">{entry.text}</p>
                  <p className="proposal-rationale">
                    <time dateTime={entry.createdAt}>{shortDate(entry.createdAt)}</time>
                    {entry.kind === "condensed" ? " · condensed" : ""}
                  </p>
                </div>
                <div className="proposal-actions">
                  <button
                    aria-label="Remove entry"
                    className="ghost-action"
                    disabled={busy}
                    onClick={() => void remove(entry)}
                    type="button"
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
        </Disclosure>
      ) : null}

      <label className="intent-field" htmlFor="intent-draft">
        Add to what you are looking for
        <textarea
          aria-describedby="intent-draft-hint"
          className="notes-input"
          id="intent-draft"
          onChange={(event) => setDraft(event.target.value)}
          rows={3}
          value={draft}
        />
      </label>
      <p className="chip-field-hint" id="intent-draft-hint">
        For example: &ldquo;forward-deployed engineering roles where I work with customers,
        not a pure backend job&rdquo;. Each entry is kept; add another when your mind changes.
      </p>
      <div className="extract-row">
        <button
          className="ghost-action"
          disabled={busy || !draft.trim()}
          onClick={() => void add()}
          type="button"
        >
          Add
        </button>
        {entries !== null ? (
          <p className="pref-hint">
            <span>
              {activeWords} of {condenseAt} words
            </span>
            {longEnough ? <span> Long enough to condense into one note.</span> : null}
          </p>
        ) : null}
        {canCondense && condensed === null ? (
          <button
            className={longEnough ? "primary-action" : "ghost-action"}
            disabled={busy}
            onClick={() => void condense()}
            type="button"
          >
            Condense
          </button>
        ) : null}
      </div>
      {condensed !== null ? (
        <div>
          <label className="intent-field" htmlFor="intent-condensed">
            Condensed note
            <textarea
              aria-describedby="intent-condensed-hint"
              className="notes-input"
              id="intent-condensed"
              onChange={(event) => setCondensed(event.target.value)}
              rows={8}
              value={condensed}
            />
          </label>
          <p className="chip-field-hint" id="intent-condensed-hint">
            Edit anything the draft got wrong. Approving keeps this note in place of the
            entries above; they are archived, not deleted.
          </p>
          <div className="extract-row">
            <button
              className="primary-action"
              disabled={busy || !condensed.trim()}
              onClick={() => void approve()}
              type="button"
            >
              Approve
            </button>
            <button
              className="ghost-action"
              disabled={busy}
              onClick={() => setCondensed(null)}
              type="button"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
