import { Fragment } from "react";
import type { ModelRole, ObsFlow } from "@/lib/api";
import { InfoTip } from "@/components/info-tip";
import { AGENT_TIP, DONE_TIP, toolTip } from "@/lib/flow-glossary";
import { formatSeconds, slowLevel } from "@/lib/format-seconds";

const ARROW_TIP =
  "Time from the last step finishing to this one finishing: the model deciding what to do, plus the tool running.";

function Arrow({ seconds }: { seconds: number }) {
  return (
    <li aria-hidden="false" className="flow-arrow" title={ARROW_TIP}>
      <span className="flow-time" data-slow={slowLevel(seconds)} data-testid="flow-time">
        {formatSeconds(seconds)}
      </span>
      <span aria-hidden="true" className="flow-line" />
    </li>
  );
}

function Box({
  children,
  tip,
  tipLabel,
  tone,
}: {
  children: React.ReactNode;
  tip: string;
  tipLabel: string;
  tone: string;
}) {
  return (
    <li className={`flow-box flow-${tone}`} data-testid="flow-box">
      {children}
      <InfoTip label={`About ${tipLabel}`}>{tip}</InfoTip>
    </li>
  );
}

const ROLES = [
  ["agent", "agent"],
  ["tool", "tool"],
  ["ok", "done"],
  ["refused", "refused"],
  ["error", "error"],
] as const;
const STEP_SCALE = [
  ["1", "3–5 s"],
  ["2", "5–8 s"],
  ["3", "8–10 s"],
  ["4", "10 s+"],
] as const;

/** What the colours mean, so a box or a slow step is readable without hovering anything. One
 * compact key, right-aligned under the runs: the box roles, then the step-time scale. */
export function FlowLegend() {
  return (
    <div aria-label="Colour key" className="flow-legend" role="group">
      {ROLES.map(([tone, label]) => (
        <span className="flow-key" key={tone}>
          <span aria-hidden="true" className={`flow-chip flow-${tone}`} />
          {label}
        </span>
      ))}
      <span className="flow-key-gap">Step time</span>
      {STEP_SCALE.map(([level, label]) => (
        <span className="flow-time" data-slow={level} key={level}>
          {label}
        </span>
      ))}
    </div>
  );
}

/** One agent run: the agent, each tool it called in order, and how it ended, joined by
 * arrows. The time on an arrow is the step's wall time; the small caption in a box is the
 * tool's own time, which for local lookups is a rounding error next to the model's. */
export function AgentFlows({ label = "Agent runs", runs }: { label?: string; runs: ObsFlow[] }) {
  return (
    <ul aria-label={label} className="flow-runs">
      {runs.map((run) => (
        <li className="flow-run" key={`${run.agent}|${run.startedAt}`}>
          <p className="flow-head">
            <strong>{run.agent}</strong> · {run.model} · {run.requests}{" "}
            {run.requests === 1 ? "request" : "requests"} · {formatSeconds(run.totalS)} total ·{" "}
            <time dateTime={run.startedAt}>{new Date(run.startedAt).toLocaleString()}</time>
          </p>
          <ol className="flow">
            <Box tip={AGENT_TIP(run.agent)} tipLabel={`${run.agent} agent`} tone="agent">
              <span className="flow-name">{run.agent}</span>
              <span className="flow-sub">starts</span>
            </Box>
            {run.steps.map((step, index) => (
              <FlowStep index={index} key={`${step.tool}-${index}`} step={step} />
            ))}
            <Arrow seconds={run.endS} />
            <Box
              tip={DONE_TIP(run.outcome)}
              tipLabel="done"
              tone={run.outcome === "ok" ? "ok" : "error"}
            >
              <span className="flow-name">done · {run.outcome}</span>
            </Box>
          </ol>
        </li>
      ))}
    </ul>
  );
}

function FlowStep({ step }: { step: ObsFlow["steps"][number]; index: number }) {
  const tone = step.outcome === "ok" ? "tool" : step.outcome === "refused" ? "refused" : "error";
  return (
    <>
      <Arrow seconds={step.stepS} />
      <Box tip={toolTip(step.tool)} tipLabel={step.tool} tone={tone}>
        <span className="flow-name">{step.tool}</span>
        <span className="flow-sub" data-slow={slowLevel(step.toolS)}>
          {formatSeconds(step.toolS)}
          {step.outcome === "ok" ? "" : ` · ${step.outcome}`}
        </span>
      </Box>
    </>
  );
}

/** Each model call a run makes (triage, match, and the other roles) as boxes joined by
 * arrows, in the Agent runs style: green when the role's backend is ready, red when not. */
export function ModelRoleFlow({ roles }: { roles: ModelRole[] }) {
  return (
    <ol aria-label="Model roles" className="flow">
      {roles.map((role, index) => {
        const ready = role.reachable && role.model_present;
        const missingKey = /no OpenRouter key/i.test(role.detail);
        const state = ready ? "ready" : missingKey ? "no key" : "unavailable";
        return (
          <Fragment key={role.role}>
            {index > 0 ? (
              <li aria-hidden="true" className="flow-arrow">
                <span className="flow-line" />
              </li>
            ) : null}
            <Box
              tip={`${role.model} on ${role.kind}${role.detail ? `: ${role.detail}` : ""}`}
              tipLabel={`${role.role} model`}
              tone={ready ? "ok" : "error"}
            >
              <span className="flow-name">{role.role}</span>
              <span className="flow-sub">{state}</span>
            </Box>
          </Fragment>
        );
      })}
    </ol>
  );
}
