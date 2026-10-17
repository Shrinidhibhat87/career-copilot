"use client";

import { useId, useState, type ReactNode } from "react";

import { InfoTip } from "@/components/info-tip";

/** A heading that hides its own contents until asked. Collapsed is the resting state for
 * everything a user tunes once: the page then reads as a short list of what it can do.
 *
 * The body is hidden, not unmounted, so a value typed into a collapsed field survives the
 * collapse and a validation error inside one still exists in the DOM — `forceOpen` is what
 * reveals it after a failed save. The explanation goes behind the ⓘ rather than onto the
 * page, which is the whole point: the heading says what the section is, the ⓘ says whether
 * you need it. */
export function Disclosure({
  children,
  defaultOpen = false,
  forceOpen = false,
  headingId,
  info,
  level = "section",
  onOpen,
  title,
}: {
  children: ReactNode;
  defaultOpen?: boolean;
  forceOpen?: boolean;
  headingId?: string;
  info?: ReactNode;
  level?: "section" | "group";
  /** Called when the user opens it, so a parent can start fetching only once it is wanted. */
  onOpen?: () => void;
  title: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [forcedBefore, setForcedBefore] = useState(forceOpen);
  const bodyId = useId();
  // A section holding an error opens itself the moment the error appears; closing it again
  // stays the user's move, so this reacts to the edge rather than to `forceOpen` being true.
  // Adjusted during render, not in an effect: React re-runs this render with the new state
  // before touching the DOM, so the section never paints closed with an error inside it.
  if (forceOpen !== forcedBefore) {
    setForcedBefore(forceOpen);
    if (forceOpen) setOpen(true);
  }

  const Heading = level === "section" ? "h2" : "h3";
  return (
    <div className={`disclosure disclosure-${level}`}>
      <div className="pref-heading-row">
        <Heading id={headingId}>
          <button
            aria-controls={bodyId}
            aria-expanded={open}
            className="disclosure-toggle"
            onClick={() => {
              setOpen(!open);
              if (!open) onOpen?.();
            }}
            type="button"
          >
            <span aria-hidden="true" className="disclosure-chevron">
              ›
            </span>
            {title}
          </button>
        </Heading>
        {info ? <InfoTip label={`About ${title}`}>{info}</InfoTip> : null}
      </div>
      <div className="disclosure-body" hidden={!open} id={bodyId}>
        {children}
      </div>
    </div>
  );
}
