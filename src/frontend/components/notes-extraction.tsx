"use client";

import { useState } from "react";

import { Disclosure } from "@/components/disclosure";
import { extractPreferences, type FieldChange, type Preferences } from "@/lib/api";

type Props = {
  notes: string;
  onNotesChange: (notes: string) => void;
  /** Called once per accepted change. Edits the form in place; nothing persists here. */
  onAccept: (change: FieldChange) => void;
  preferences: Preferences;
};

function describe(change: FieldChange): string {
  const field = change.field.replace(/_/g, " ");
  if (change.action === "set") return `Set ${field} to ${change.label}`;
  if (change.action === "remove") return `Remove ${change.label} from ${field}`;
  return `Add ${change.label} to ${field}`;
}

/** The notes field, and the opt-in pass that proposes structured changes from it.
 *
 * Notes reach the prompt verbatim whether or not this is ever used. What this adds is a
 * way to lift the nuance in them into structured fields — one accepted change at a time,
 * never automatically, because these fields drive deterministic filtering.
 */
export function NotesExtraction({ notes, onNotesChange, onAccept, preferences }: Props) {
  const [changes, setChanges] = useState<FieldChange[] | null>(null);
  const [cost, setCost] = useState(0);
  const [status, setStatus] = useState<"idle" | "working">("idle");
  const [error, setError] = useState<string | null>(null);

  async function extract() {
    setStatus("working");
    setError(null);
    try {
      const result = await extractPreferences(notes, preferences);
      setChanges(result.changes);
      setCost(result.costEur);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Something went wrong.");
    } finally {
      setStatus("idle");
    }
  }

  function settle(change: FieldChange, accepted: boolean) {
    if (accepted) onAccept(change);
    setChanges((current) => (current ?? []).filter((other) => other !== change));
  }

  return (
    <section className="pref-section" aria-labelledby="notes-heading">
      <Disclosure
        forceOpen={Boolean(error) || (changes !== null && changes.length > 0)}
        headingId="notes-heading"
        info="Anything the fields above cannot express. This text is sent to the model with every posting, exactly as written, and one paid model call can read it back into the fields."
        title="Notes"
      >
        <textarea
          aria-label="Notes"
          className="notes-input"
          onChange={(event) => onNotesChange(event.target.value)}
          rows={6}
          value={notes}
        />
        <div className="extract-row">
          <button
            className="ghost-action"
            disabled={status === "working" || !notes.trim()}
            onClick={() => void extract()}
            type="button"
          >
            {status === "working" ? "EXTRACTING…" : "EXTRACT INTO PREFERENCES"}
          </button>
          <p className="pref-hint">
            Makes one paid model call. Nothing is changed until you accept a suggestion, and
            nothing is saved until you press Save.
          </p>
        </div>

        <p aria-live="polite" className="live-status">
          {status === "working" ? "Reading your notes." : error ? error : ""}
        </p>
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}

        {changes !== null && !error ? (
          changes.length === 0 ? (
            <p className="pref-hint">
              Nothing to propose from these notes{cost > 0 ? ` (€${cost.toFixed(4)})` : ""}.
            </p>
          ) : (
            <>
              <p className="chips-label">
                PROPOSED — €{cost.toFixed(4)} · ACCEPT EACH ONE SEPARATELY
              </p>
              <ul className="proposal-list">
                {changes.map((change, index) => (
                  <li className="proposal" key={`${change.field}-${change.value}-${index}`}>
                    <div>
                      <p className="proposal-summary">{describe(change)}</p>
                      {change.rationale ? (
                        <p className="proposal-rationale">{change.rationale}</p>
                      ) : null}
                    </div>
                    <div className="proposal-actions">
                      <button
                        className="ghost-action"
                        onClick={() => settle(change, true)}
                        type="button"
                      >
                        ACCEPT
                      </button>
                      <button
                        className="ghost-action"
                        onClick={() => settle(change, false)}
                        type="button"
                      >
                        REJECT
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )
        ) : null}
      </Disclosure>
      <p className="pref-hint">
        {preferences.version === 0
          ? "These preferences have never been saved; they come from your config file and resume."
          : `Saved version ${preferences.version}.`}
      </p>
    </section>
  );
}
