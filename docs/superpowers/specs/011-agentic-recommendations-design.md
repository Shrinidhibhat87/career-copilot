# Agentic Recommendations — Design

> The user says in plain words what they want, adds sample roles, and chats with a Copilot. Agents turn that into a search profile, a shortlist, and actions on the list. Core rule: a small local model must never be able to break a run, so every agent has hard limits and a fallback. Builds on `Preferences` (005-preferences), the scoring flow (002-scoring-and-digest), the redaction gateway (004-pii-gateway), the local model setup (008-local-model-runtime), and the decisions in 010-persistence-and-lifecycle.

## Decisions

| Topic | Decision |
|---|---|
| Intent | "What you are looking for" is an append-only log, `intent_entries`. An entry is at most `MAX_ENTRY_CHARS` (2000). Removing one archives it. |
| Condense | At `CONDENSE_AT_WORDS` (1000) active words the user can draft one condensed note (`usecases/condense_intent.py`). The user edits and approves it. The old entries are archived and the note becomes an entry of kind `condensed`, at most `MAX_CONDENSED_CHARS` (4000). |
| Sample roles | The user pastes a job URL, or a title and text. A URL is resolved to board, slug and job id through `SOURCE_TYPES`. If it cannot be resolved the API answers 422 with `needsText: true`. The company board joins `source_targets`. There are no outcome tags: a role the user was rejected for is still a good example. |
| Ranking | The default `scoring.ranker` is `embedding`: a local CPU embedder (`scoring.embedding_model`, `BAAI/bge-small-en-v1.5`) ranks the pool. A job's score is 0.6 x its best match to a sample role plus 0.4 x its best match to an intent entry. With one kind only, that kind stands alone. With none, the run uses triage. |
| Vector cache | Job vectors are cached in `job_embeddings` by the hash of the text the model read and the model name. A model change re-embeds once. |
| Fallback ranker | `scoring.ranker: llm_triage` is one LLM call per candidate. It is also used when the embedding path cannot rank. |
| Profiler | `agents/profiler.py` reads active intent, sample roles and the CV and returns a `SearchProfile`. The CV and intent are redacted first, because an agent bypasses `RedactingLLMClient`. |
| Profile cache | One `search_profiles` row per distinct inputs hash. Unchanged inputs reuse the profile with no model call. |
| Profile use | `Preferences.search_profile` replaces `role_intent` in the prompt text when set. Similar companies and archetypes feed discovery (009-source-discovery). |
| Curator | `agents/curator/` reads the top `POOL_SIZE` (150) ranked jobs with `search_pool`, `get_job`, `shortlist`. It shortlists at most `MAX_SHORTLIST` (20). Limits: 15 requests, 30 tool calls. The `MatchAgent` judges the shortlist. |
| Curator fallback | On failure, timeout or a short list, the embedding order fills up the rest. |
| Match calls | `EMBEDDING_MATCH_CALL_FACTOR` is 2 on the embedding path, `MATCH_CALL_FACTOR` is 3 on triage. |
| Refresh | Saving an intent entry or a sample role calls `request_refresh`: an incremental run starts if none is active, and one more follows an active run (`refresh_pending`). Old match scores stay visible until replaced. |
| Stale app | On app load the UI calls `POST /api/runs/refresh` with `ifStale: true`. A run starts only if the last good run is older than `STALE_AFTER` (20 hours). |
| Refresh cost | A refresh starts without a cost preview, so it starts only if none of `triage`, `match`, `profiler`, `curator` is a paid model. Otherwise the answer carries the reason. |
| Roles | `profiler` and `curator` fall back to the triage role, like `discovery`. `assistant` falls back the same way, but `config.yaml` sets it to `qwen3:14b`, because the 4B triage model often answers without calling a tool. |
| Copilot | A round button on Recommendations opens a chat. `POST /api/assistant` takes the new message, the last 10 turns and the current view. The server keeps no chat state. |
| Copilot tools | `view_counts`, `set_view`, `add_intent`, `refresh_jobs`, and `exclude_roles` (012-chat-actions-and-run-feedback). The agent has no other power. |
| Asking back | If the user gives no number ("make it more"), the agent calls `view_counts`, asks "How many more?" and changes nothing. |
| Fewer than hoped | If only 4 jobs clear the bar, the agent says so and offers lower matches. It does not lower the bar. |
| Context | Active intent, the last 20 `feedback_entries`, the last 20 evidence corrections, and earlier batch notes. The CV is never sent. All of it is redacted first. |
| Saving | Every user message and reply is saved in `feedback_entries`. A failed model call still saves the user's message and answers "I could not answer just now. Your note is saved." |
| Local only | If the `assistant` role is not local, the API answers 409 "The assistant needs a local model". |
| Limits | 8 requests, 6 tool calls, 60 seconds per message. While a run is active the limit is 120 seconds and the reply has `busy: true`. |
| Tool refusals | A tool that refuses (empty text, text over 2000 characters, limit under 1) returns a string and changes nothing. `set_view` clamps the limit to 1..50. |
| Old box | `POST /api/feedback` no longer exists. Its `corrections` rows of kind `batch` are read as earlier feedback. |
| Models | vLLM: Qwen3-4B-Instruct-2507 in 4-bit AWQ with `--enable-auto-tool-choice --tool-call-parser hermes`. Ollama: `qwen3:4b-instruct`. See 008-local-model-runtime. |

## Shapes

```sql
-- migration 12: intent_entries(id, user_id, text, kind entry|condensed, created_at, archived_at)
-- migration 13: sample_roles(id, user_id, url, source, slug, source_job_id, company, title, text, added_at, archived_at)
-- migration 14: job_embeddings(text_hash, model, dim, vector, created_at)   -- key: (text_hash, model)
-- migration 15: search_profiles(id, inputs_hash UNIQUE, payload, model, created_at)
-- migration 18
CREATE TABLE feedback_entries (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
  role TEXT NOT NULL,                 -- 'user' | 'assistant'
  text TEXT NOT NULL, actions TEXT);  -- actions: JSON list on assistant rows
```

```python
# domain/search_profile.py
class SearchProfile:  # summary, role_archetypes, must_haves, dealbreakers, locations,
                      # seniority, similar_companies, search_phrases

# agents/assistant/agent.py
class AssistantLimits:  max_requests = 8; max_tool_calls = 6; max_seconds = 60.0; busy_seconds = 120.0
class AssistantDeps:    config; store; view; actions   # tools append to `actions`
```

```ts
type AssistantAction =
  | { kind: "set_view"; limit: number; includeBelow: boolean }  // limit 1..50
  | { kind: "intent_added"; text: string }
  | { kind: "refresh"; started: boolean; reason?: string };
// POST /api/assistant -> { text, actions, busy }
```

Routes: `GET/POST /api/intent`, `DELETE /api/intent/{id}`, `POST /api/intent/condense` and `/condense/approve`, `GET/POST /api/sample-roles`, `DELETE /api/sample-roles/{id}`.

## Flow

```
add intent or sample role: store it; request_refresh()     # run now, or queue one after the active run
run: profile = profiler(redact(intent, cv), roles)         # cached by inputs hash; failure -> no profile, run goes on
     ranked = embed and rank(pool)                         # cannot rank (no references, embedder error) -> llm_triage
     picks = curator(ranked[:150])                          # any failure or short list -> fill from ranked order
     match(picks) up to target x factor, stop at target above min_match_score

POST /api/assistant(message, history, view):
    role not local -> 409
    save user row
    context = redact(intent + recent feedback + evidence corrections)
    try: reply = agent.run(...)                            # limits above
    except (limit, timeout, backend down): reply = "I could not answer just now..."   # 200, nothing changed
    save assistant row with actions; return {text, actions, busy}
add_intent: store it, then request_refresh (the same path as POST /api/intent)
refresh_jobs: request_refresh(config); the reason is passed back when nothing starts
```

## Testing

CI runs every row except Manual, with `FakeLLM`, a fake embedder (`adapters/embed/fake.py`) and pydantic-ai `FunctionModel`. Correctness is judged by returned actions, stored rows and ranked order, never by reply wording.

| Layer | What it proves |
|---|---|
| pytest `test_intent_store.py`, `test_intent_api.py`, `test_intent_condense_api.py` | Entries append and archive. The old `role_intent` was seeded as entry 1. Condense archives the old entries and adds one. Over-long text is refused. |
| pytest `test_sample_roles.py`, `test_sample_roles_api.py` | URL shapes resolve to board, slug and job id. A bad URL gives 422 with `needsText`. The board joins `source_targets`. |
| pytest `test_rank.py`, `test_embed_jobs.py`, `test_job_embeddings_store.py` | The blend gives the expected order. Ties go by job id. No references gives an empty order. An unchanged posting is embedded once. |
| pytest `test_profiler.py`, `test_search_profile.py`, `test_search_profiles_store.py` | The profile comes back typed. Same inputs reuse the row. The model never sees the owner's name, email or phone. |
| pytest `test_curator.py` | A scripted shortlist is used. A broken or timed-out curator still gives a full list from embedding order. |
| pytest `test_refresh_api.py` | A save starts a run, or queues one after an active run. `ifStale` skips a recent run. A paid model blocks the refresh with a reason. |
| pytest `test_assistant.py`, `test_assistant_api.py` | "Make it more" with no number asks and returns no actions. "10 more" returns `set_view` with limit 10. "No fintech" calls `add_intent` once. A refused tool changes nothing. Both messages are saved, also when the model fails. A non-local role gives 409. `busy` and the 120 s limit apply only during a run. `/api/feedback` is gone. |
| vitest `intent-log`, `review`, `auto-refresh` | Add and remove entries. The Copilot popup sends on Enter, shows a "..." bubble, and applies `set_view`. App load calls refresh with `ifStale`. |
| Manual (local model) | Add an intent entry and a sample role, expect a re-rank. Ask "I only see 4, show more": it asks how many, then the list grows. Restart: everything is still there. |

## Non-goals

Cross-encoder reranking, multi-user, bulk save by chat, bulk dismiss beyond `exclude_roles`, changing the score threshold by chat, and streaming replies.
