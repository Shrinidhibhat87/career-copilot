"use client";

import { useState } from "react";

import type { PartName, ScoreParts } from "@/lib/api";

const RADIUS = 46;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;
const GAP = 2;

/** Order, label and ceiling of each part. The ceilings sum to 100, so a part's share of the
 * ring is its ceiling and the total in the centre is the sum of the earned points. */
export const PARTS: { key: PartName; label: string; max: number }[] = [
  { key: "skills", label: "Skills", max: 40 },
  { key: "seniority", label: "Seniority and role", max: 25 },
  { key: "domain", label: "Domain", max: 20 },
  { key: "preferences", label: "Preferences and location", max: 15 },
];

/** A match score as four named parts. Each part owns a fixed stretch of the ring (faint),
 * filled by the points it earned. The legend rows and the arcs are buttons that open that
 * part's reason; a second click closes it. */
export function ScoreDonut({ parts }: { parts: ScoreParts }) {
  const [open, setOpen] = useState<PartName | null>(null);
  const toggle = (key: PartName) => setOpen((current) => (current === key ? null : key));
  const total = PARTS.reduce((sum, part) => sum + parts[part.key].points, 0);
  const detail = PARTS.find((part) => part.key === open) ?? null;

  const arcs = PARTS.map((part, index) => {
    const span = (part.max / 100) * CIRCUMFERENCE;
    const before = PARTS.slice(0, index).reduce((sum, p) => sum + p.max, 0);
    return {
      ...part,
      offset: (before / 100) * CIRCUMFERENCE,
      track: Math.max(span - GAP, 0),
      filled: Math.max((parts[part.key].points / part.max) * span - GAP, 0),
    };
  });

  return (
    <div className="score-donut">
      <div className="donut-row">
        <svg
          aria-label={`Match score ${total} out of 100`}
          className="donut-ring"
          role="img"
          viewBox="0 0 120 120"
        >
          {arcs.map((arc) => (
            <g
              className={`part-arc part-${arc.key}${open === arc.key ? " part-arc-active" : ""}${
                open && open !== arc.key ? " part-arc-dim" : ""
              }`}
              data-part={arc.key}
              key={arc.key}
              onClick={() => toggle(arc.key)}
              transform="rotate(-90 60 60)"
            >
              <circle
                className="part-track"
                cx="60"
                cy="60"
                fill="none"
                r={RADIUS}
                strokeDasharray={`${arc.track} ${CIRCUMFERENCE}`}
                strokeDashoffset={-arc.offset}
              />
              <circle
                className="part-fill"
                cx="60"
                cy="60"
                fill="none"
                r={RADIUS}
                strokeDasharray={`${arc.filled} ${CIRCUMFERENCE}`}
                strokeDashoffset={-arc.offset}
              />
              <title>{`${arc.label}: ${parts[arc.key].points} / ${arc.max}`}</title>
            </g>
          ))}
          <text className="donut-total" textAnchor="middle" x="60" y="62">
            {total}
          </text>
          <text className="donut-caption" textAnchor="middle" x="60" y="77">
            of 100
          </text>
        </svg>

        <ul className="donut-legend">
          {PARTS.map((part) => (
            <li key={part.key}>
              <button
                aria-expanded={open === part.key}
                aria-pressed={open === part.key}
                className="donut-legend-item"
                onClick={() => toggle(part.key)}
                type="button"
              >
                <span aria-hidden="true" className={`donut-swatch part-swatch-${part.key}`} />
                <span className="donut-legend-label">{part.label}</span>
                <span className="donut-legend-count">
                  {parts[part.key].points} / {part.max}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {detail ? (
        <p className="part-reason" role="status">
          <strong>{detail.label}.</strong> {parts[detail.key].reason}
        </p>
      ) : null}
    </div>
  );
}
