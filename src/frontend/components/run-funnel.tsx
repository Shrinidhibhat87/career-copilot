"use client";

import { useState, type CSSProperties } from "react";

import type { PipelineRun, RunFunnel, RunStage } from "@/lib/api";

const STAGE_LABELS: Record<string, string> = {
  discover: "Discover",
  profile: "Profile",
  score: "Score",
  digest: "Digest",
};
const STAGE_COLORS: Record<string, string> = {
  discover: "#2a78d6",
  profile: "#1baf7a",
  score: "#eb6834",
  digest: "#9a968c",
};

const RADIUS = 46;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

// Green is a good outcome, amber wants a look, red is a failure, slate is "filtered out as
// intended". Each slice is also named and counted in the legend, never colour alone.
const GOOD = "#1baf7a";
const INFO = "#2a78d6";
const NEUTRAL = "#9a968c";
const ATTENTION = "#d99a1b";
const BAD = "#d64545";
const SLATE = "#5f6b85";
const PURPLE = "#7a5bd1";
const PALE = "#c9c5bb";

/** Whether a run has anything for this section to show (runs recorded before stage times
 * and funnels existed have neither). */
export function hasRunDetails(run: PipelineRun): boolean {
  return (run.stages?.length ?? 0) > 0 || Boolean(run.funnel);
}

export function formatSeconds(seconds: number): string {
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} min ${Math.round(seconds - minutes * 60)} s`;
}

/** One horizontal bar for the whole run, each stage as wide as the time it took. */
function StageBar({ stages }: { stages: RunStage[] }) {
  const total = stages.reduce((sum, stage) => sum + (stage.seconds ?? 0), 0);
  return (
    <div className="stage-times">
      <div
        aria-label="Time spent in each stage"
        className="stage-bar"
        role="img"
      >
        {stages.map((stage) => {
          const open = stage.seconds === null;
          return (
            <span
              className={open ? "stage-seg stage-open" : "stage-seg"}
              key={stage.stage}
              style={
                {
                  "--seg": STAGE_COLORS[stage.stage] ?? NEUTRAL,
                  flexGrow: open ? 0.04 : Math.max(stage.seconds ?? 0, total * 0.01),
                } as CSSProperties
              }
              title={`${STAGE_LABELS[stage.stage] ?? stage.stage}: ${
                open ? "did not finish" : formatSeconds(stage.seconds ?? 0)
              }`}
            />
          );
        })}
      </div>
      <ul className="stage-legend">
        {stages.map((stage) => (
          <li key={stage.stage}>
            <span
              aria-hidden="true"
              className="stage-swatch"
              style={{ "--seg": STAGE_COLORS[stage.stage] ?? NEUTRAL } as CSSProperties}
            />
            <span>{STAGE_LABELS[stage.stage] ?? stage.stage}</span>
            <strong>
              {stage.seconds === null ? "did not finish" : formatSeconds(stage.seconds)}
            </strong>
            {stage.seconds !== null && total > 0 ? (
              <span className="meta">{Math.round((stage.seconds / total) * 100)}%</span>
            ) : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

type Slice = {
  key: string;
  label: string;
  count: number;
  color: string;
  /** What this slice means, shown when it is opened. */
  explain: string;
  /** Names behind the count, when the run recorded them. */
  items?: string[];
};

/** A ring plus a bulleted legend. A click on a slice or a legend row opens what it means;
 * the legend is the keyboard-operable form and writes every count out. */
function FunnelDonut({
  title,
  unit,
  slices,
}: {
  title: string;
  unit: string;
  slices: Slice[];
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const shown = slices.filter((slice) => slice.count > 0);
  const total = shown.reduce((sum, slice) => sum + slice.count, 0);
  if (total === 0) return null;

  const arcs = shown.map((slice, index) => {
    const before = shown.slice(0, index).reduce((sum, item) => sum + item.count, 0);
    const share = (slice.count / total) * CIRCUMFERENCE;
    return {
      slice,
      length: Math.max(share - (shown.length > 1 ? 2 : 0), 0.5),
      offset: (before / total) * CIRCUMFERENCE,
    };
  });
  const open = shown.find((slice) => slice.key === selected) ?? null;
  const toggle = (key: string) =>
    setSelected((current) => (current === key ? null : key));

  return (
    <section className="run-donut" aria-label={title}>
      <h4>{title}</h4>
      <div className="donut-row">
        <svg
          aria-label={`${total} ${unit}, split by outcome`}
          className="donut-ring"
          role="img"
          viewBox="0 0 120 120"
        >
          {arcs.map(({ slice, length, offset }) => {
            const active = selected === slice.key;
            return (
              <circle
                className={`donut-arc${active ? " donut-arc-active" : ""}${
                  selected && !active ? " donut-arc-dim" : ""
                }`}
                cx="60"
                cy="60"
                data-segment={slice.key}
                fill="none"
                key={slice.key}
                onClick={() => toggle(slice.key)}
                r={RADIUS}
                stroke={slice.color}
                strokeDasharray={`${length} ${CIRCUMFERENCE}`}
                strokeDashoffset={-offset}
                transform="rotate(-90 60 60)"
              >
                <title>{`${slice.label}: ${slice.count}`}</title>
              </circle>
            );
          })}
          <text className="donut-total" textAnchor="middle" x="60" y="62">
            {total}
          </text>
          <text className="donut-caption" textAnchor="middle" x="60" y="77">
            {unit}
          </text>
        </svg>
        <ul className="donut-legend">
          {shown.map((slice) => (
            <li key={slice.key}>
              <button
                aria-pressed={selected === slice.key}
                className="donut-legend-item"
                onClick={() => toggle(slice.key)}
                type="button"
              >
                <span
                  aria-hidden="true"
                  className="donut-swatch"
                  style={{ background: slice.color }}
                />
                <span className="donut-legend-label">{slice.label}</span>
                <span className="donut-legend-count">
                  {slice.count} · {Math.round((slice.count / total) * 100)}%
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      {open ? (
        <div className="donut-detail">
          <div className="donut-detail-head">
            <h3>
              {open.label} ({open.count})
            </h3>
            <button
              aria-label="Close details"
              className="donut-close"
              onClick={() => toggle(open.key)}
              type="button"
            >
              ✕
            </button>
          </div>
          <p className="meta">{open.explain}</p>
          {open.items?.length ? (
            <ul className="donut-companies">
              {open.items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function sourceSlices(funnel: RunFunnel): Slice[] {
  const { skipped, failed } = funnel.discover;
  // A funnel recorded before the board list existed only has counts.
  const boards = funnel.discover.boards ?? [];
  return [
    {
      key: "searched",
      label: "Searched",
      count: boards.length,
      color: GOOD,
      explain: "Boards that answered. The number is how many jobs each one returned.",
      items: boards.map((b) => `${b.target} — ${b.fetched} jobs`),
    },
    {
      key: "skipped",
      label: "Skipped",
      count: skipped.length,
      color: ATTENTION,
      explain:
        "Not searched this time: a board is left alone for 48 hours after a search (cooldown), or the run already had enough new jobs (target met).",
      items: skipped.map((s) => `${s.target} — ${s.reason}`),
    },
    {
      key: "failed",
      label: "Failed",
      count: failed.length,
      color: BAD,
      explain: "The board could not be fetched. Other boards were kept.",
      items: failed.map((f) => `${f.target} — ${f.error || "no detail"}`),
    },
  ];
}

function jobSlices(funnel: RunFunnel): Slice[] {
  const d = funnel.discover;
  return [
    {
      key: "passed",
      label: "Passed the filters",
      count: Math.max(d.fetched - d.rejected - d.review, 0),
      color: GOOD,
      explain: `Fit your location, language, seniority, role and stack rules. ${d.qualified} of them were new this run.`,
    },
    {
      key: "review",
      label: "Needs a look",
      count: d.review,
      color: ATTENTION,
      explain:
        "The rules could not decide (for example an unfamiliar title). These are held back and not scored.",
    },
    {
      key: "rejected",
      label: "Rejected",
      count: d.rejected,
      color: SLATE,
      explain:
        "A job board lists every role a company hires for, in every office, so most of what it returns does not fit your rules. A high share here is normal. Each job is counted once, under its first reason.",
      items: (d.rejectReasons ?? []).map(
        (item) => `${item.reason} — ${item.count} ${item.count === 1 ? "job" : "jobs"}`,
      ),
    },
  ];
}

function scoreSlices(funnel: RunFunnel): Slice[] {
  const s = funnel.score;
  return [
    {
      key: "above",
      label: `Matched, ${s.minScore}+`,
      count: s.above,
      color: GOOD,
      explain: `Scored at or above ${s.minScore}, so they count as recommendations.`,
    },
    {
      key: "below",
      label: `Matched, under ${s.minScore}`,
      count: Math.max(s.matched - s.above, 0),
      color: INFO,
      explain: "Scored, but under the bar. They show only under “Show lower matches”.",
    },
    {
      key: "capped",
      label: "Over the company limit",
      count: s.capped,
      color: PURPLE,
      explain:
        "Only a few jobs per company are scored in one run, so one employer cannot fill the list.",
    },
    {
      key: "failed",
      label: "Scoring failed",
      count: s.failed,
      color: BAD,
      explain: "The model's answer could not be used for these jobs.",
    },
    {
      key: "left",
      label: "Not reached",
      count: Math.max(s.candidates - s.capped - s.matched - s.failed, 0),
      color: PALE,
      explain: "Ranked lower and left for a later run, or outside the curated shortlist.",
    },
  ];
}

const STOP_NOTES: Record<string, { tone: string; title: string; note: string }> = {
  "target reached": {
    tone: "good",
    title: "Target reached",
    note: "It found as many recommendations as you asked for, so it stopped early.",
  },
  "out of candidates": {
    tone: "attention",
    title: "Ran out of jobs to score",
    note: "Every job that passed the filters was scored. More boards, or looser filters, would give it more to work with.",
  },
  "match-call cap": {
    tone: "attention",
    title: "Hit the scoring limit",
    note: "It scored as many jobs as one run allows and stopped before reaching the target.",
  },
  budget: {
    tone: "bad",
    title: "Stopped by the spending limit",
    note: "The run's cost ceiling was reached. The jobs scored so far were kept.",
  },
};

/** The run's answer to "did it find enough, and why did it stop": the count against the
 * target as a bar, and the stop reason as a labelled badge with a plain sentence. */
function RunResult({ score }: { score: RunFunnel["score"] }) {
  const share = score.target > 0 ? Math.min(score.above / score.target, 1) : 0;
  const stop = STOP_NOTES[score.stoppedBy] ?? {
    tone: "attention",
    title: score.stoppedBy,
    note: "",
  };
  return (
    <div className="run-result">
      <div className="run-result-count">
        <strong>{score.above}</strong>
        <span>
          of {score.target} recommendations
          <small>scored {score.minScore} or higher</small>
        </span>
      </div>
      <div
        aria-label={`${score.above} of ${score.target} recommendations`}
        aria-valuemax={score.target}
        aria-valuemin={0}
        aria-valuenow={score.above}
        className="run-result-bar"
        role="progressbar"
      >
        <span data-tone={stop.tone} style={{ width: `${share * 100}%` }} />
      </div>
      <p className="run-result-stop" data-tone={stop.tone}>
        <span className="run-result-badge">{stop.title}</span>
        {stop.note}
      </p>
    </div>
  );
}

/** How long each stage took and what each step let through. Renders nothing for a run
 * recorded before either existed. */
export function RunFunnelDetails({ run }: { run: PipelineRun }) {
  const stages = run.stages ?? [];
  const funnel = run.funnel;
  if (!hasRunDetails(run)) return null;
  return (
    <div className="run-funnel">
      {stages.length ? (
        <>
          <p className="run-total">
            <strong>{formatSeconds(stages.reduce((sum, s) => sum + (s.seconds ?? 0), 0))}</strong>
            {run.state === "running" ? " so far" : " in total"}
          </p>
          <p className="meta">
            Run {run.runId} · started {new Date(run.startedAt).toLocaleString()}
          </p>
          <StageBar stages={stages} />
        </>
      ) : null}
      {funnel ? (
        <>
          <h3>What this run did</h3>
          <div className="run-donuts">
            <FunnelDonut slices={sourceSlices(funnel)} title="Sources" unit="sources" />
            <FunnelDonut slices={jobSlices(funnel)} title="Jobs fetched" unit="jobs" />
            <FunnelDonut slices={scoreSlices(funnel)} title="Scoring" unit="to score" />
          </div>
          <RunResult score={funnel.score} />
        </>
      ) : null}
    </div>
  );
}
