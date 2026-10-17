"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { authenticate, getAuthStatus, importAccount, type AuthStatus } from "@/lib/api";
import { routeAfterSignIn } from "@/lib/landing";

type Mode = "signin" | "create";

const MIN_PASSCODE = 6;

export function SignInActions() {
  const router = useRouter();
  const passcodeRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [mode, setMode] = useState<Mode>("signin");
  const [name, setName] = useState("");
  const [passcode, setPasscode] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function enter() {
    router.push(await routeAfterSignIn());
  }

  useEffect(() => {
    let live = true;
    getAuthStatus()
      .then(async (found) => {
        if (!live) return;
        if (found.user !== null) {
          // Already signed in (a reload, or the wordmark link): straight to the app.
          await enter();
          return;
        }
        setStatus(found);
        setMode(found.accounts.length === 0 ? "create" : "signin");
      })
      .catch(() => live && setError("Could not reach the server. Try again."));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function flip(next: Mode) {
    setMode(next);
    setError(null);
    setPasscode("");
    setConfirm("");
    setFile(null);
  }

  async function submit() {
    if (busy) return;
    const creating = mode === "create";
    if (creating && passcode.length < MIN_PASSCODE) {
      setError(`The passcode needs at least ${MIN_PASSCODE} characters.`);
      return;
    }
    if (creating && passcode !== confirm) {
      setError("The two passcodes do not match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (creating && file) await importAccount(name.trim(), passcode, file);
      else await authenticate(creating ? "register" : "login", name.trim(), passcode);
      await enter();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "That did not work. Try again.");
      setBusy(false);
    }
  }

  const creating = mode === "create";
  const hasAccounts = (status?.accounts.length ?? 0) > 0;

  return (
    <section className="sign-in-panel" aria-labelledby="sign-in-heading">
      <div className="sign-in-card">
        {status === null && error === null ? (
          <div className="sign-in-skeleton" aria-hidden="true" />
        ) : null}
        {status !== null ? (
          <div className="sign-in-body" key={mode}>
            <h2 id="sign-in-heading">{creating ? "Create your account" : "Welcome back"}</h2>
            {creating && status.adoptsExisting && !file ? (
              <p className="sign-in-note">
                Your existing CV, preferences and decisions will move into this account.
              </p>
            ) : null}
            {!creating && hasAccounts ? (
              <ul className="account-chips" aria-label="Accounts on this machine">
                {status.accounts.map((account) => (
                  <li key={account}>
                    <button
                      type="button"
                      className="account-chip"
                      aria-pressed={name === account}
                      onClick={() => {
                        setName(account);
                        passcodeRef.current?.focus();
                      }}
                    >
                      {account}
                    </button>
                  </li>
                ))}
              </ul>
            ) : null}
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void submit();
              }}
            >
              <label htmlFor="username">Username</label>
              <input
                id="username"
                name="username"
                autoComplete="username"
                autoCapitalize="none"
                spellCheck={false}
                placeholder="e.g. anna"
                value={name}
                onChange={(event) => setName(event.target.value)}
                required
              />

              <label htmlFor="passcode">Passcode</label>
              <input
                id="passcode"
                name="passcode"
                type="password"
                ref={passcodeRef}
                autoComplete={creating ? "new-password" : "current-password"}
                placeholder={creating ? `At least ${MIN_PASSCODE} characters` : "Enter your passcode"}
                value={passcode}
                onChange={(event) => setPasscode(event.target.value)}
                required
              />

              {creating ? (
                <>
                  <label htmlFor="confirm">Confirm passcode</label>
                  <input
                    id="confirm"
                    name="confirm"
                    type="password"
                    autoComplete="new-password"
                    placeholder="Type it once more"
                    value={confirm}
                    onChange={(event) => setConfirm(event.target.value)}
                    required
                  />
                  <div className="import-row">
                    <input
                      ref={fileRef}
                      className="import-input"
                      type="file"
                      accept=".db"
                      aria-label="Export file"
                      onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                    />
                    {file ? (
                      <>
                        <span className="import-name">{file.name}</span>
                        <button
                          type="button"
                          className="import-clear"
                          onClick={() => {
                            setFile(null);
                            if (fileRef.current) fileRef.current.value = "";
                          }}
                        >
                          Remove
                        </button>
                      </>
                    ) : (
                      <button type="button" className="import-pick" onClick={() => fileRef.current?.click()}>
                        Import from an export file
                      </button>
                    )}
                  </div>
                  <p className="sign-in-note">
                    There is no reset. Everything stays on this machine, so keep the passcode safe.
                  </p>
                </>
              ) : null}

              <button aria-busy={busy} className="primary-action" type="submit" disabled={busy}>
                {creating ? "Create account" : "Sign in"}
              </button>
              {busy ? (
                <p className="live-status" aria-live="polite">
                  Checking your profile…
                </p>
              ) : null}
            </form>
            <div className="new-account">
              <span>{creating ? "Already have an account?" : "New to Career Copilot?"}</span>
              <button type="button" onClick={() => flip(creating ? "signin" : "create")} disabled={busy}>
                {creating ? "Sign in" : "Create account"}
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
    </section>
  );
}
