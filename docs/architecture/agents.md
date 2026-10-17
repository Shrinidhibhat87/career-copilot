# Agents

Which steps are model-driven, which tools they get, and what they must return.

Back to the [architecture index](../architecture.md).

## Agents

Agents are leaves — never in control flow of collect, store, filter, or digest, which
stay deterministic. `triage` and `match` call `LLMClient`. The tool-calling agents
(`agents/discovery`, `agents/curator`, `agents/assistant`) use `pydantic_ai.Agent`
directly, bounded by step, time and euro limits.

| Agent | Tools | Output contract |
|---|---|---|
| Triage (`agents/triage.py`) | none | `{relevance 0-100, one sentence}` on every survivor, cheap model |
| Match (`agents/match.py`) | none; resume sent with line IDs | four named parts (skills 40, seniority and role 25, domain 20, preferences and location 15), reasons, `evidence_refs[]`; code adds the total |
| Outline (`agents/outline.py`) | none | `{responsibilities[], requirements[]}` for a card, read on its first open on the `match` model and kept in `job_outlines`; code drops any sentence not found in the posting |
| Profiler, Condenser | none | preferences and intent drafts from the CV and the user's notes |
| Curator (`agents/curator/`) | search profile, sample roles | search profile that shapes retrieval |
| Assistant (`agents/assistant/`) | store reads, corrections | feedback chat over saved jobs |
| Discovery (`agents/discovery/`) | `web_search`, `probe_ats`, `sample_jobs`, `known_companies`, `propose_source` | board proposals in `source_proposals`, budget-gated by `discovery:` |

`evidence_refs` forces every claimed match to cite a resume line; an empty or unknown
ID is a `MissingEvidenceError`, not a low score. The system recommends; it never
applies on the user's behalf.
