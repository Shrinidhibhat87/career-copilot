"use client";

import { useEffect, useId, useRef, useState } from "react";

/** A small ⓘ that reveals a longer explanation on click, so a page can stay clean and
 * still say what a control does. A button, not a hover title: it works on touch and by
 * keyboard, and the text is announced as the button's described content.
 *
 * It dismisses like a popover — a click anywhere else, or Escape — because a tip that can
 * only be closed from the button it came out of leaves the page covered in open notes.
 * Clicking a second ⓘ closes the first for free: that click is outside this one. */
export function InfoTip({ label, children }: { label: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLSpanElement>(null);
  const bodyId = useId();

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      // `pointerdown`, not `click`: the tip should go as the press lands, and a `click`
      // listener on the document also fires for the toggle itself, which would reopen it.
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span className="info-tip" ref={root}>
      <button
        aria-controls={bodyId}
        aria-expanded={open}
        aria-label={label}
        className="info-tip-button"
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        i
      </button>
      {open ? (
        <span className="info-tip-body" id={bodyId} role="note">
          {children}
        </span>
      ) : null}
    </span>
  );
}
