"use client";

import { useEffect, useState } from "react";

import {
  addSampleRoleText,
  addSampleRoleUrl,
  getSampleRoles,
  NeedsTextError,
  removeSampleRole,
  ValidationError,
  type SampleRole,
} from "@/lib/api";

const message = (failure: unknown) => {
  if (failure instanceof ValidationError) {
    return Object.values(failure.errors).join(" ") || failure.message;
  }
  return failure instanceof Error ? failure.message : "Something went wrong.";
};

/** Postings the user points at as "more like this". A link is read from its job board;
 * when the server cannot read it (a closed posting, an unknown board, a board that is
 * down), the screen asks for the posting's text instead rather than dropping the role. */
export function SampleRoles() {
  const [roles, setRoles] = useState<SampleRole[]>([]);
  const [link, setLink] = useState("");
  // Set once the server has asked for pasted text; holds its reason.
  const [needsText, setNeedsText] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [company, setCompany] = useState("");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    void getSampleRoles()
      .then((loaded) => {
        if (live) setRoles(loaded);
      })
      .catch((failure: unknown) => {
        if (live) setError(message(failure));
      });
    return () => {
      live = false;
    };
  }, []);

  // Adding the same posting twice returns the existing row, so it is replaced, not listed
  // a second time.
  const keep = (role: SampleRole) =>
    setRoles((current) => [...current.filter((other) => other.id !== role.id), role]);

  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (failure) {
      if (failure instanceof NeedsTextError) setNeedsText(failure.message);
      else setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  const addLink = () =>
    run(async () => {
      keep(await addSampleRoleUrl(link.trim()));
      setLink("");
      setNeedsText(null);
    });

  const addPasted = () =>
    run(async () => {
      keep(await addSampleRoleText(title.trim(), text.trim(), company.trim() || undefined));
      setNeedsText(null);
      setLink("");
      setTitle("");
      setCompany("");
      setText("");
    });

  const remove = (role: SampleRole) =>
    run(async () => {
      await removeSampleRole(role.id);
      setRoles((current) => current.filter((other) => other.id !== role.id));
    });

  return (
    <div>
      <h3 className="chips-label" id="sample-roles-heading">
        Sample roles you like
      </h3>
      {roles.length > 0 ? (
        <ul aria-labelledby="sample-roles-heading" className="proposal-list">
          {roles.map((role) => (
            <li className="proposal" key={role.id}>
              <p className="proposal-summary">
                {role.company} —{" "}
                {role.url ? (
                  <a href={role.url} rel="noreferrer" target="_blank">
                    {role.title}
                  </a>
                ) : (
                  role.title
                )}
              </p>
              <div className="proposal-actions">
                <button
                  aria-label="Remove sample role"
                  className="ghost-action"
                  disabled={busy}
                  onClick={() => void remove(role)}
                  type="button"
                >
                  Remove
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : null}

      <label className="chip-field-label" htmlFor="sample-role-link">
        Job link
      </label>
      <form
        className="chip-add"
        onSubmit={(event) => {
          event.preventDefault();
          if (link.trim() && !busy) void addLink();
        }}
      >
        <input
          disabled={busy}
          id="sample-role-link"
          onChange={(event) => setLink(event.target.value)}
          placeholder="https://jobs.ashbyhq.com/…"
          type="url"
          value={link}
        />
        <button className="ghost-action" disabled={busy || !link.trim()} type="submit">
          Add role
        </button>
      </form>

      {needsText !== null ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (title.trim() && text.trim() && !busy) void addPasted();
          }}
        >
          <p className="chip-field-hint">{needsText}</p>
          <div className="chip-add">
            <label className="chip-field-label" htmlFor="sample-role-title">
              Job title
            </label>
            <input
              id="sample-role-title"
              onChange={(event) => setTitle(event.target.value)}
              value={title}
            />
            <label className="chip-field-label" htmlFor="sample-role-company">
              Company (optional)
            </label>
            <input
              id="sample-role-company"
              onChange={(event) => setCompany(event.target.value)}
              value={company}
            />
          </div>
          <label className="intent-field" htmlFor="sample-role-text">
            Job text
            <textarea
              className="notes-input"
              id="sample-role-text"
              onChange={(event) => setText(event.target.value)}
              rows={6}
              value={text}
            />
          </label>
          <div className="extract-row">
            <button
              className="ghost-action"
              disabled={busy || !title.trim() || !text.trim()}
              type="submit"
            >
              Add pasted role
            </button>
          </div>
        </form>
      ) : null}

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
