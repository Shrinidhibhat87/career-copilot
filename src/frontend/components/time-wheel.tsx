"use client";

import { useEffect, useRef, useState } from "react";

import { Modal } from "@/components/modal";
import { to12, to24, type Clock12 } from "@/lib/schedule-time";

const ITEM = 40;
const HOURS = Array.from({ length: 12 }, (_, i) => String(i + 1));
const MINUTES = ["00", "30"];
const HALVES = ["AM", "PM"];

/** One scrolling column that snaps to the centre row, like a phone's time picker. Choose by
 * scrolling, clicking a row, or the arrow keys. */
function Wheel({
  label,
  items,
  selected,
  onSelect,
}: {
  label: string;
  items: string[];
  selected: string;
  onSelect: (item: string) => void;
}) {
  const list = useRef<HTMLDivElement>(null);
  const settle = useRef<ReturnType<typeof setTimeout>>(undefined);
  const index = items.indexOf(selected);

  useEffect(() => {
    if (list.current) list.current.scrollTop = index * ITEM;
  }, [index]);
  useEffect(() => () => clearTimeout(settle.current), []);

  const step = (by: number) => {
    const next = items[Math.min(items.length - 1, Math.max(0, index + by))];
    if (next !== selected) onSelect(next);
  };

  return (
    <div
      aria-label={label}
      className="wheel"
      onKeyDown={(event) => {
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          step(event.key === "ArrowDown" ? 1 : -1);
        }
      }}
      onScroll={(event) => {
        const top = event.currentTarget.scrollTop;
        clearTimeout(settle.current);
        // Read the row once scrolling has stopped, not on every pixel of it.
        settle.current = setTimeout(() => {
          const picked = items[Math.min(items.length - 1, Math.max(0, Math.round(top / ITEM)))];
          if (picked !== selected) onSelect(picked);
        }, 90);
      }}
      ref={list}
      role="listbox"
      tabIndex={0}
    >
      {items.map((item) => (
        <button
          aria-selected={item === selected}
          className="wheel-item"
          key={item}
          onClick={() => onSelect(item)}
          role="option"
          tabIndex={-1}
          type="button"
        >
          {item}
        </button>
      ))}
    </div>
  );
}

/** Pick a time on the hour or half hour, 12-hour clock. Done returns "HH:MM"; Cancel and
 * Escape keep what was there. */
export function TimeWheel({
  title,
  value,
  onDone,
  onCancel,
}: {
  title: string;
  value: string;
  onDone: (time: string) => void;
  onCancel: () => void;
}) {
  const [clock, setClock] = useState<Clock12>(() => to12(value));
  return (
    <Modal label={title} onClose={onCancel}>
      <div className="wheel-panel">
        <h3>{title}</h3>
        <div className="wheel-row">
          <Wheel
            items={HOURS}
            label="Hour"
            onSelect={(hour) => setClock({ ...clock, hour: Number(hour) })}
            selected={String(clock.hour)}
          />
          <Wheel
            items={MINUTES}
            label="Minute"
            onSelect={(minute) => setClock({ ...clock, minute: minute as Clock12["minute"] })}
            selected={clock.minute}
          />
          <Wheel
            items={HALVES}
            label="AM or PM"
            onSelect={(half) => setClock({ ...clock, pm: half === "PM" })}
            selected={clock.pm ? "PM" : "AM"}
          />
        </div>
        <div className="wheel-actions">
          <button onClick={onCancel} type="button">
            Cancel
          </button>
          <button data-primary="true" onClick={() => onDone(to24(clock))} type="button">
            Done
          </button>
        </div>
      </div>
    </Modal>
  );
}
