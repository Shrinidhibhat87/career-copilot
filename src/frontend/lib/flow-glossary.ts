/** One-line explanations for the boxes in an agent run, shown behind each box's ⓘ. A tool the
 * map does not know still gets a line, so a newly added tool never shows an empty tip. */
const TOOLS: Record<string, string> = {
  search_pool: "Tool: searches the scored job pool for roles matching the user's request.",
  get_job: "Tool: reads the full details of one job from the pool.",
  shortlist: "Tool: adds a job to the shortlist. It is refused when a rule says the job cannot go on it.",
};

export const AGENT_TIP = (agent: string) =>
  `Agent: ${agent} starts the run. It decides which tool to call next, and each decision is a model request.`;

export const DONE_TIP = (outcome: string) =>
  outcome === "ok"
    ? "Done: the run finished normally."
    : `Done: the run ended with the outcome "${outcome}".`;

export const toolTip = (tool: string) => TOOLS[tool] ?? `Tool: ${tool}`;
