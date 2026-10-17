"use client";

import { useState } from "react";

import { Disclosure } from "@/components/disclosure";
import { deleteAccount } from "@/lib/api";

/** Two collapsed cards. Download is a plain link: the browser sends the session cookie, the server streams a
 * whole copy of this account's database. Nothing is uploaded anywhere. Delete opens a
 * confirmation popup that asks for the passcode again, since a browser left open must not be
 * enough to erase an account. */
export function AccountExport() {
  const [confirming, setConfirming] = useState(false);
  const [passcode, setPasscode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function cancel() {
    setConfirming(false);
    setPasscode("");
    setError(null);
  }

  async function erase() {
    setBusy(true);
    setError(null);
    const refusal = await deleteAccount(passcode);
    if (refusal !== null) {
      setError(refusal);
      setBusy(false);
      return;
    }
    try {
      window.sessionStorage.clear();
    } catch {
      // Storage blocked: nothing to clear.
    }
    // A full load, not a client push: nothing of the deleted account may stay on screen.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/");
  }

  return (
    <>
      <section className="run-card account-export">
        <Disclosure title="Download your data">
          <p className="settings-note">
            Download everything in this account (CV, preferences, decisions and run history) as one
            file. To use it on another machine, create an account there and choose &ldquo;Import
            from an export file&rdquo;. The OpenRouter key is not included.
          </p>
          <a className="export-link" href="/api/account/export" download>
            Download my data
          </a>
        </Disclosure>
      </section>
      <section className="run-card danger-zone">
        <Disclosure title="Delete this account">
          <p>
            Permanently removes this account and everything in it: CV, preferences, decisions and
            run history. This cannot be undone. Download your data first if you may want it back.
          </p>
          <button className="danger-button" type="button" onClick={() => setConfirming(true)}>
            Delete my account
          </button>
        </Disclosure>
      </section>
      {confirming ? (
        <div className="danger-backdrop" onClick={() => !busy && cancel()}>
          <form
            aria-describedby="danger-text"
            aria-labelledby="danger-heading"
            aria-modal="true"
            className="danger-modal"
            role="alertdialog"
            onClick={(event) => event.stopPropagation()}
            onKeyDown={(event) => {
              if (event.key === "Escape" && !busy) cancel();
            }}
            onSubmit={(event) => {
              event.preventDefault();
              void erase();
            }}
          >
            <h2 id="danger-heading">Delete this account?</h2>
            <p id="danger-text">
              Everything in it is erased for good and you will be signed out. Type your passcode to
              confirm.
            </p>
            <label htmlFor="delete-passcode">Passcode</label>
            <input
              id="delete-passcode"
              type="password"
              autoComplete="current-password"
              value={passcode}
              onChange={(event) => setPasscode(event.target.value)}
              required
              autoFocus
            />
            {error ? (
              <p className="danger-error" role="alert">
                {error}
              </p>
            ) : null}
            <div className="danger-actions">
              <button aria-busy={busy} className="danger-button" type="submit" disabled={busy}>
                {busy ? "Deleting…" : "Delete everything"}
              </button>
              <button className="danger-cancel" type="button" onClick={cancel} disabled={busy}>
                Cancel
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </>
  );
}
