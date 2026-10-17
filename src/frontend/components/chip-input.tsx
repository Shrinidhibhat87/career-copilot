"use client";

import { useId, useState } from "react";

import type { Term } from "@/lib/api";

type Props = {
  label: string;
  hint?: string;
  values: Term[];
  onChange: (values: Term[]) => void;
  /** Refuse to add beyond this many, and show the count. */
  max?: number;
  error?: string;
};

/** Add/remove chips. Chips display labels; what leaves this control is always a term.
 *
 * The label is only ever what the server sent for a term it already knows. A hand-typed
 * value has no label yet, so it shows exactly what was typed and is lowercased into a
 * term on submit — any term is legal, because a term is only ever a matcher.
 */
export function ChipInput({ label, hint, values, onChange, max, error }: Props) {
  const [draft, setDraft] = useState("");
  const inputId = useId();
  const errorId = `${inputId}-error`;
  const atLimit = max !== undefined && values.length >= max;

  function add() {
    const term = draft.trim().toLowerCase().split(/\s+/).join(" ");
    if (!term || atLimit) return;
    if (!values.some((value) => value.term === term)) {
      onChange([...values, { term, label: draft.trim(), fromResume: false }]);
    }
    setDraft("");
  }

  return (
    <div className="chip-field">
      <div className="chip-field-head">
        <label className="chip-field-label" htmlFor={inputId}>
          {label}
        </label>
        {max !== undefined ? (
          <span className="chip-count">
            {values.length} of {max}
          </span>
        ) : null}
      </div>
      {hint ? <p className="chip-field-hint">{hint}</p> : null}
      <ul className="chips chips-editable">
        {values.map((value) => (
          <li className="chip" key={value.term}>
            {value.label}
            {value.fromResume ? (
              <span className="chip-badge" title="Filled in from your resume">
                RESUME
              </span>
            ) : null}
            <button
              aria-label={`Remove ${value.label}`}
              className="chip-remove"
              onClick={() => onChange(values.filter((other) => other.term !== value.term))}
              type="button"
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      <div className="chip-add">
        <input
          aria-describedby={error ? errorId : undefined}
          aria-invalid={error ? true : undefined}
          disabled={atLimit}
          id={inputId}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key !== "Enter") return;
            // The field sits inside the preferences form; Enter adds a chip here rather
            // than submitting everything.
            event.preventDefault();
            add();
          }}
          placeholder={atLimit ? "Full — remove one with its ×" : "Type and press Enter"}
          value={draft}
        />
        <button className="ghost-action" disabled={atLimit} onClick={add} type="button">
          ADD
        </button>
      </div>
      {error ? (
        <p className="form-error" id={errorId} role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
