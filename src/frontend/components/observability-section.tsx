"use client";

import { useEffect, useState } from "react";

import { AgentFlows, FlowLegend } from "@/components/agent-flow";
import { Disclosure } from "@/components/disclosure";
import { InfoTip } from "@/components/info-tip";
import {
  getObsCost,
  getObsFlows,
  getObsSeries,
  getObsSessions,
  getObsSummary,
  getObsTools,
  obsLogUrl,
  type ObsCost,
  type ObsFlow,
  type ObsSeriesPoint,
  type ObsSession,
  type ObsSummary,
  type ObsToolRow,
} from "@/lib/api";
import { formatSeconds } from "@/lib/format-seconds";
import { euros } from "@/lib/money";
import { linePaths } from "@/lib/observability-chart";

const TODAY = 1;
// The latest run is shown, with the two before it tucked away; older ones live in the log.
const FLOW_LIMIT = 3;
const WINDOWS = [
  { days: 1, label: "Today" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
] as const;
const BOX = { width: 560, height: 160, pad: 12 };

type Data = {
  // The window these numbers were fetched for, so a slow switch never mixes windows.
  days: number;
  summary: ObsSummary;
  tools: ObsToolRow[];
  series: ObsSeriesPoint[];
  flows: ObsFlow[];
  // Null when the cost could not be read: the rest of the dashboard still shows.
  cost: ObsCost | null;
};

const seconds = formatSeconds;
const plain = (value: number | null) => (value === null ? "—" : String(value));

function duration(value: number | null): string {
  if (value === null) return "still open";
  const total = Math.round(value);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  return m > 0 ? `${m}m ${total % 60}s` : `${total}s`;
}

/** What the models and agent tools have been doing: speed, volume and failures, from rows
 * the app recorded itself. Never shows a prompt or a reply, because none is stored.
 *
 * Closed until opened, and nothing is fetched before then: the body mounts on first open, so
 * visiting Settings costs no observability requests. */
export function ObservabilitySection() {
  const [opened, setOpened] = useState(false);
  return (
    <section className="run-card obs" aria-label="Observability">
      <Disclosure onOpen={() => setOpened(true)} title="Observability">
        {opened ? <ObservabilityBody /> : null}
      </Disclosure>
    </section>
  );
}

function ObservabilityBody() {
  const [days, setDays] = useState<number>(7);
  const [data, setData] = useState<Data | null>(null);
  const [sessions, setSessions] = useState<ObsSession[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    let current = true;
    // Runs are the latest few, not a total over the window, so only Today shows them: under a
    // wider window the same handful would sit beside numbers that cover many more calls.
    const flows =
      days === TODAY ? getObsFlows(days, FLOW_LIMIT) : Promise.resolve([] as ObsFlow[]);
    const cost = getObsCost(days).catch(() => null);
    Promise.all([
      getObsSummary(days),
      getObsTools(days),
      getObsSeries(days),
      flows,
      cost,
    ])
      .then(([summary, tools, series, flows, cost]) => {
        if (!current) return;
        setData({ days, summary, tools, series, flows, cost });
        setError("");
      })
      .catch((reason: Error) => current && setError(reason.message));
    return () => {
      current = false;
    };
  }, [days]);

  useEffect(() => {
    let current = true;
    getObsSessions()
      .then((rows) => current && setSessions(rows))
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, []);

  const empty = data !== null && data.summary.tiles.calls === 0 && data.summary.rows.length === 0;

  return (
    <>
      <div className="obs-windows" role="group" aria-label="Window">
        {WINDOWS.map((window) => (
          <button
            aria-pressed={days === window.days}
            className="obs-window"
            key={window.days}
            onClick={() => setDays(window.days)}
            type="button"
          >
            {window.label}
          </button>
        ))}
      </div>
      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      {data && !empty ? <Dashboard data={data} sessions={sessions} /> : null}
      {empty ? <p className="pref-hint">No calls recorded yet</p> : null}
      {sessions.length > 0 ? <Sessions sessions={sessions} /> : null}
    </>
  );
}

function Sessions({ sessions }: { sessions: ObsSession[] }) {
  return (
    <Disclosure
      info="One row per time the app was started. Download gives you that session's log file. A session marked unclean ended without a proper shutdown."
      level="group"
      title="Sessions"
    >
      <ul className="run-history">
        {sessions.map((session) => (
          <li key={session.id}>
            <span>
              <time dateTime={session.startedAt}>
                {new Date(session.startedAt).toLocaleString()}
              </time>{" "}
              · {duration(session.durationS)} · {session.outcome ?? "running"}
              {session.profile ? ` · ${session.profile}` : ""}
            </span>
            <span className="obs-log">
              <code>{session.logPath.split("/").pop()}</code>
              {session.hasLog ? (
                <a download href={obsLogUrl(session.id)}>
                  Download log
                </a>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </Disclosure>
  );
}

const TIPS: Record<string, string> = {
  "Output tokens/s":
    "How fast the models write. Output tokens divided by the time spent waiting for them, across every call in the window. Higher is faster.",
  "Avg latency":
    "The average time of one model request: one prompt sent, one reply back. It is not per job and not per run. A run such as finding new jobs makes many requests (a triage and a match for each candidate), so its total is many times this number.",
  "Total tokens":
    "All the text the models read (tokens in) plus all they wrote (tokens out) in this window.",
  Spent:
    "What the cloud models billed in this window. Local models cost nothing, so a window run only on this machine reads €0.00. Calls recorded before cost tracking began count as €0.00.",
  Calls: "Model requests plus agent runs in this window. A retry counts as its own call.",
  "Error rate":
    "The share of calls that failed or came back unusable, for example a timeout, a rate limit or an invalid reply.",
};

function Dashboard({ data, sessions }: { data: Data; sessions: ObsSession[] }) {
  const { tiles, rows } = data.summary;
  const lines = linePaths(data.series, BOX);
  const days = new Set(data.series.map((point) => point.day)).size;
  return (
    <>
      <dl className="obs-tiles">
        <Tile label="Output tokens/s" value={String(tiles.tokensPerS)} />
        <Tile label="Avg latency" value={seconds(tiles.avgLatencyS)} />
        <Tile label="Total tokens" value={tiles.totalTokens.toLocaleString("en")} />
        {data.cost ? (
          <Tile label="Spent" value={euros(data.cost.totalEur)} />
        ) : null}
        <Tile label="Calls" value={String(tiles.calls)} />
        <Tile label="Error rate" value={`${Math.round(tiles.errorRate * 100)}%`} />
      </dl>

      {data.cost ? <CostBlock cost={data.cost} /> : null}

      <Disclosure
        info="One row per model and stage. Tokens/s p50 is the typical speed; latency p50 and p95 are the typical and the slow-end time of one request."
        level="group"
        title="Models"
      >
      <table className="obs-table" aria-label="Calls per model and stage">
        <thead>
          <tr>
            <th>Model</th>
            <th>Stage</th>
            <th>Calls</th>
            <th>Tokens in</th>
            <th>Tokens out</th>
            <th>Tokens/s p50</th>
            <th>Latency p50</th>
            <th>Latency p95</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={`${row.model}|${row.backend}|${row.stage}`}>
              <td>{row.model}</td>
              <td>{row.stage}</td>
              <td>{row.calls}</td>
              <td>{row.tokensIn}</td>
              <td>{row.tokensOut}</td>
              <td>{plain(row.tokensPerSP50)}</td>
              <td>{seconds(row.latencyP50)}</td>
              <td>{seconds(row.latencyP95)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </Disclosure>

      {data.tools.length > 0 ? (
        <Disclosure
          info="How often each tool was called and its slowest call. Tools are local lookups, so they are fast; the time an agent spends is mostly the model deciding between them, which the arrows above show."
          level="group"
          title="Tool totals"
        >
          <table className="obs-table" aria-label="Calls per tool">
            <thead>
              <tr>
                <th>Agent</th>
                <th>Tool</th>
                <th>Calls</th>
                <th>Slowest</th>
                <th>Refused</th>
                <th>Errors</th>
              </tr>
            </thead>
            <tbody>
              {data.tools.map((tool) => (
                <tr key={`${tool.agent}|${tool.tool}`}>
                  <td>{tool.agent}</td>
                  <td>{tool.tool}</td>
                  <td>{tool.calls}</td>
                  <td>{seconds(tool.slowestS)}</td>
                  <td>{tool.refused}</td>
                  <td>{tool.errors}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Disclosure>
      ) : null}

      <Disclosure
        info="Average output tokens per second for each model, one point per day. It needs two days of data to draw a line."
        level="group"
        title="Speed over time"
      >
      {days >= 2 ? (
        <figure className="obs-chart">
          <svg
            aria-label="Daily tokens per second by model"
            role="img"
            viewBox={`0 0 ${BOX.width} ${BOX.height}`}
          >
            {lines.map((line, index) => (
              <g className={`obs-line obs-line-${index % 6}`} key={line.model}>
                <polyline
                  fill="none"
                  points={line.points.map((p) => `${p.x},${p.y}`).join(" ")}
                />
                {line.points.map((p) => (
                  <circle cx={p.x} cy={p.y} key={p.day} r={3}>
                    <title>{`${line.model} · ${p.day} · ${p.value} tokens/s`}</title>
                  </circle>
                ))}
              </g>
            ))}
          </svg>
          <figcaption className="obs-legend">
            {lines.map((line, index) => (
              <span className={`obs-key obs-line-${index % 6}`} key={line.model}>
                {line.model}
              </span>
            ))}
          </figcaption>
        </figure>
      ) : (
        <p className="pref-hint">Trend appears after two days of data.</p>
      )}
      </Disclosure>

      {data.days === TODAY && data.flows.length > 0 ? (
        <Runs runs={data.flows} sessions={sessions} />
      ) : null}
    </>
  );
}

function Runs({ runs, sessions }: { runs: ObsFlow[]; sessions: ObsSession[] }) {
  const [latest, ...earlier] = runs;
  const log = sessions.find((session) => session.hasLog);
  return (
    <Disclosure
      info="Each agent run, step by step. A box is the agent or a tool it called; the time on an arrow is how long that step took, the model deciding plus the tool running. A refused box means a tool declined, which is the agent being told a rule, not a failure. Hover the ⓘ on a box for what it is."
      level="group"
      title="Agent runs"
    >
      <AgentFlows runs={[latest]} />
      {earlier.length > 0 ? (
        <div className="flow-earlier">
          <Disclosure level="group" title={`Earlier runs (${earlier.length})`}>
            <AgentFlows label="Earlier agent runs" runs={earlier} />
          </Disclosure>
        </div>
      ) : null}
      <FlowLegend />
      <p className="flow-footnote">
        Older runs are in the session log.{" "}
        {log ? (
          <a download href={obsLogUrl(log.id)}>
            Download latest log
          </a>
        ) : null}
      </p>
    </Disclosure>
  );
}

function CostBlock({ cost }: { cost: ObsCost }) {
  const cloud = cost.byBackend.filter((row) => row.eur > 0);
  return (
    <Disclosure
      info="What the models cost in this window, from the amount each provider billed. Only cloud calls cost money; local models are free."
      level="group"
      title="Cost"
    >
      {cost.totalEur === 0 ? (
        <p className="pref-hint">No cloud spend in this window.</p>
      ) : (
        <p className="pref-hint">
          {euros(cost.totalEur)} on{" "}
          {cloud.map((row) => row.backend).join(", ")}.
        </p>
      )}
      <table className="obs-table" aria-label="Cost per model and stage">
        <thead>
          <tr>
            <th>Model</th>
            <th>Backend</th>
            <th>Stage</th>
            <th>Calls</th>
            <th>Cost</th>
          </tr>
        </thead>
        <tbody>
          {cost.byModel.map((row) => (
            <tr key={`${row.model}|${row.backend}|${row.stage}`}>
              <td>{row.model}</td>
              <td>{row.backend}</td>
              <td>{row.stage}</td>
              <td>{row.calls}</td>
              <td>{euros(row.eur)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {cost.byDay.length > 1 ? (
        <ul className="run-history" aria-label="Cost per day">
          {cost.byDay.map((day) => (
            <li key={day.day}>
              <span>{day.day}</span>
              <span>{euros(day.eur)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </Disclosure>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="obs-tile">
      <dt>
        {label}
        <InfoTip label={`About ${label}`}>{TIPS[label]}</InfoTip>
      </dt>
      <dd>{value}</dd>
    </div>
  );
}
