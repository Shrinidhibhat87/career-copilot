import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // `career-copilot ui` serves this build from FastAPI, one origin, no node process on
  // the user's machine. Every route here is a client component over the HTTP API, so
  // there is nothing for a server runtime to do.
  output: "export",
  // `next dev` otherwise writes its own AGENTS.md and CLAUDE.md here, which this
  // repo already provides at the root.
  agentRules: false,
};

export default nextConfig;
