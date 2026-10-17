# career-copilot — job search agent

> **Status: original plan (historical).** The built system has since moved on: there are
> more agents (profiler, curator, assistant, discovery), accounts with one database per
> person, a browser UI, and no connections matching, libSQL or scheduled Actions runs.
> For what exists today read [`architecture.md`](architecture.md) and the specs it maps.

A personal agent that finds well-matched roles, scores them strictly against the
resume, and returns an application **strategy**. It never applies on anyone's behalf.

Structure and components: [`architecture.md`](architecture.md).
Matching criteria: [`../profile/preferences.example.md`](../profile/preferences.example.md).
Experience inventory: the CV you upload on the CV page (stored per account, never in git).

---

## Goal

Five strong applications, not two hundred weak ones. The agent's job is to remove
browsing: surface the few roles worth the effort, say why, name the gaps, find the
warm intro, and hand over a plan the user executes.

## What it produces

1. **Ranked digest** — score, why it fits, gaps, location/language warnings.
2. **Application strategy** per top role — what to emphasize, which resume evidence
   leads, which gaps to preempt, what to ask.
3. **Referral plan** — connections at the company, one outreach draft each.
4. **Tailoring brief** — input to the existing LaTeX workflow in `CLAUDE.md`,
   produced only when the user decides to apply.

## Scope boundaries

- Recommends and strategizes. Never submits an application.
- Never invents skills, metrics, employers, or language levels absent from the resume.
- A hard-filter failure is never outweighed by a high match score.
- Ambiguous eligibility or German-level wording goes to manual review, not a silent reject.
- Personal use first, but no profile identity in code or schema — a second user is a
  second profile directory.

## Key decisions

| Decision | Choice | Reason |
|---|---|---|
| Architecture | Hexagonal, 4 ports | Local ↔ cloud swap and source churn are both "same contract, different implementation" |
| Agents | 4, at decision edges only | Judgment needs an agent; collect/store/filter/digest do not |
| Sources | ATS boards first, tiered ladder | Free, structured, stable, startup-heavy — matches the preference for smaller companies |
| Budget | €0–5/month | Hard filters before tokens; cheap triage before deep scoring; early exit when the day's target is met |
| Store | SQLite, canonical | Single writer, durable, trivially testable; libSQL adapter for cloud runs |
| Tracker | SQLite + generated board | Notion deferred; the port slot stays open |
| Runtime | Local CLI first, GitHub Actions later | Value before ops; the switch is configuration |
| Method | TDD, plus E2E on every milestone | Idempotency and scoring strictness are only real if asserted |

## Reliability boundaries

| Stage | Deterministic | Note |
|---|---|---|
| Collect | No | External sites change; every attempt is logged in `source_runs` |
| Store / dedupe | Yes | DB constraints, not in-memory checks |
| Hard filters | Yes | Rules only, run before any model call |
| Score | Constrained | Temperature 0, schema-validated, version-stamped, calibration-tested |
| Strategy | No | Judgment output, reviewed by the user |
| Digest / board | Yes | Query plus template |

Scoring and strategy are the only intentionally fuzzy layers.

## Milestones

Each is a branch with its own spec, written when the branch opens.

1. Domain, SQLite store, ATS adapter, dedupe, hard filters, `list`
2. Triage, `MatchAgent`, calibration fixtures, Markdown digest
3. `CompanyResearchAgent`, `StrategyAgent`, connections matching, outreach drafts
4. Application board
5. libSQL adapter and scheduled Actions run
6. Aggregators, Apify, `DiscoveryAgent` — only if tier-0 recall proves insufficient

## First step

Milestone 1. Prove that two consecutive runs create zero duplicates and that the
hard filters reject what `preferences.md` says they must. Everything else waits.

## Open items

- Confirm ATS watchlist seed (~30 Berlin/remote companies).
- Pick the cheap triage model and calibrate its threshold against the labeled set.
- Decide the connections-CSV refresh cadence; LinkedIn exports go stale.
