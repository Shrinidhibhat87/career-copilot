"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { getAuthStatus, signOut } from "@/lib/api";

/** Who is signed in, and what they can do from anywhere: Profile, Update CV, Sign out.
 * Renders nothing on the sign-in page, where nobody is. */
export function AccountMenu() {
  const [user, setUser] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    getAuthStatus()
      .then((status) => setUser(status.user))
      .catch(() => setUser(null));
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onPointer = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onPointer);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onPointer);
    };
  }, [open]);

  if (user === null) return null;

  async function leave() {
    setBusy(true);
    setError(null);
    const refusal = await signOut();
    if (refusal !== null) {
      setError(refusal);
      setBusy(false);
      return;
    }
    // The next person starts clean: no "already refreshed" flag from this browser session.
    try {
      window.sessionStorage.clear();
    } catch {
      // Storage blocked: nothing to clear.
    }
    // A full load, not a client push: every screen's state belongs to the person who left.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign("/");
  }

  const initial = user.slice(0, 1).toUpperCase();
  return (
    <div className="account-menu" ref={root}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={`Account: ${user}`}
        className="profile-pill"
        data-open={open}
        type="button"
        onClick={() => setOpen(!open)}
      >
        <span aria-hidden="true" className="avatar">
          {initial}
        </span>
        <span className="account-name">{user}</span>
        <span aria-hidden="true" className="caret">
          ▾
        </span>
      </button>
      {open ? (
        <div className="profile-menu" role="menu">
          <div className="profile-menu-head">
            <span aria-hidden="true" className="avatar avatar-large">
              {initial}
            </span>
            <div>
              <div className="profile-menu-name">{user}</div>
              <div className="profile-menu-sub">Signed in</div>
            </div>
          </div>
          <div className="profile-menu-group">
            <Link href="/resume" role="menuitem" onClick={() => setOpen(false)}>
              Profile
            </Link>
            <Link href="/resume" role="menuitem" onClick={() => setOpen(false)}>
              Update CV
            </Link>
          </div>
          <div className="profile-menu-group profile-menu-last">
            <button
              aria-busy={busy}
              className="signout-button"
              disabled={busy}
              role="menuitem"
              type="button"
              onClick={() => void leave()}
            >
              {busy ? "Signing out…" : "Sign out"}
            </button>
            {error ? (
              <span className="account-error" role="alert">
                {error}
              </span>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
