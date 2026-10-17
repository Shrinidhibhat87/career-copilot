"use client";

import { useEffect, useRef, type ReactNode } from "react";

/** A panel over the page. Escape closes it, and so does a click that both starts and ends
 * on the backdrop, so dragging a text selection out of the panel does not. */
export function Modal({
  label,
  onClose,
  children,
}: {
  label: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const pressedBackdrop = useRef(false);
  const panel = useRef<HTMLDivElement>(null);

  useEffect(() => panel.current?.focus(), []);

  return (
    <div
      aria-label={label}
      aria-modal="true"
      className="card-overlay"
      onClick={(event) => {
        if (pressedBackdrop.current && event.target === event.currentTarget) onClose();
        pressedBackdrop.current = false;
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
      onMouseDown={(event) => {
        pressedBackdrop.current = event.target === event.currentTarget;
      }}
      role="dialog"
    >
      <div className="modal-body" ref={panel} tabIndex={-1}>
        {children}
      </div>
    </div>
  );
}
