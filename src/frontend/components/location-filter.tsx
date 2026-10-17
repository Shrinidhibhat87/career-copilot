"use client";

import { useEffect, useRef, useState } from "react";

export type LocationOption = { city: string; count: number };

/** The Location filter: a trigger that opens a multi-select of cities with counts.
 * Nothing selected means every location. */
export function LocationFilter({
  options,
  selected,
  onChange,
}: {
  options: LocationOption[];
  selected: string[];
  onChange: (cities: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

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

  const label =
    selected.length === 0
      ? "ALL"
      : selected.length === 1
        ? selected[0].toUpperCase()
        : `${selected.length} SELECTED`;

  function toggle(city: string) {
    onChange(
      selected.includes(city)
        ? selected.filter((item) => item !== city)
        : [...selected, city],
    );
  }

  return (
    <div className="location-filter" ref={root}>
      <button
        aria-expanded={open}
        aria-haspopup="true"
        aria-label={`Location: ${label}`}
        className="location-trigger"
        data-active={selected.length > 0}
        type="button"
        onClick={() => setOpen(!open)}
      >
        <span aria-hidden="true">⌖ </span>
        LOCATION: {label} <span aria-hidden="true">▾</span>
      </button>
      {open ? (
        <div
          aria-label="Choose locations"
          className="location-menu"
          role="group"
        >
          {options.length ? (
            <ul>
              {options.map((option) => (
                <li key={option.city}>
                  <label>
                    <input
                      checked={selected.includes(option.city)}
                      type="checkbox"
                      onChange={() => toggle(option.city)}
                    />
                    <span className="location-name">{option.city}</span>
                    <span className="location-count">{option.count}</span>
                  </label>
                </li>
              ))}
            </ul>
          ) : (
            <p className="location-none">No locations in the list.</p>
          )}
          <div className="location-foot">
            <button type="button" onClick={() => onChange([])}>
              Clear
            </button>
            <button
              className="location-done"
              type="button"
              onClick={() => setOpen(false)}
            >
              Done
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
