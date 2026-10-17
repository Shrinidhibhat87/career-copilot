# Chat Actions and Run Feedback — Design

> The Copilot says "done" only when something changed, and says "finished" when a search ends. Before this, "remove Manager roles" only added a free-text line, and a search started from chat could not be seen or heard back from. Builds on 011-agentic-recommendations (Copilot, curator), 006-runs-and-observability, 010-persistence-and-lifecycle (dismiss, Recycle bin) and 005-preferences.

## Decisions

| Topic | Decision |
|---|---|
| Remove roles | A new tool `exclude_roles(terms)` does two things: the words join `excluded_role_terms` (a new preferences version, then `apply_preferences` re-filters every stored job), and every posting the page lists whose title has a word is dismissed, so it lands in the Recycle bin and can be restored. |
| Scope of the bin | Only postings the page lists now (above the bar, or all when "lower matches" is on). Saved and applied postings are never touched. Below-bar postings are filtered by the new rule but not binned. |
| Limits | At most 5 words per message, 40 characters each. If more than `MAX_DISMISS` (100) listed postings match, the tool refuses and changes nothing. |
| Matching | `domain.filters.matching_terms`, the same matcher the hard filters use, so the bin and the filter agree. |
| `add_intent` | Unchanged. It is for what the user wants more of. The prompt sends "remove or hide" requests to `exclude_roles`. |
| Run id | The `refresh` action carries `runId`. A refused start carries `reason`, which the chat shows. |
| Following a run | The chat follows a search it started with `watchRun` (the poll loop `RefreshJobs` already had, now shared in `lib/run-watch.ts`). While it runs the chat shows `stage completed/total` with a link to Settings. When it ends the chat posts one message and reloads the list. |
| Queued | If the start was "queued after the active run", the chat says so and follows the run already active. |
| Result message | "Found N new roles" (ids not listed before) or "Nothing new found", then `matched` scored and `above` cleared the match bar. It names the limit that applied: the per-run target (`batch_size`), and how many were held back by `max_per_company`. A failed run shows its `failureMessage`. |
| Run summary | A pipeline run's `summary` ends in `candidates= matched= above=A/T capped=` (`ScoreReport.capped` is new). The page reads it; no new column. |
| Saved note | The message is written to `feedback_entries` through `POST /api/assistant/note` (500 characters), so the assistant's own history knows how the search ended. |
| Chat observability | The route passes one `AgentRecording` to both the agent (tool rows) and `converse` (run row). Chat turns now show in Settings, joined to their tool calls. |
| Curator | The prompt asks for at most 3 searches and batched `shortlist` calls, and the preview grows from 15 to 25 postings so most picks need no search. No schema change: shortlist reasons stay out of the database (tool arguments never leave `agents/recording.py`). |

## Shapes

```python
# agents/assistant/tools.py
class Action:  kind: "set_view" | "intent_added" | "refresh" | "jobs_changed"
               run_id: int | None   # refresh      (wire: runId)
               terms: list[str]     # jobs_changed
               count: int           # jobs_changed: postings moved to the bin
```

```ts
type AssistantAction =
  | { kind: "set_view"; limit: number; includeBelow: boolean }
  | { kind: "intent_added"; text: string }
  | { kind: "refresh"; started: boolean; reason?: string; runId?: number }
  | { kind: "jobs_changed"; terms: string[]; count: number };
// POST /api/assistant/note { text } -> 201 { saved: true }
```

## Flow

```
"remove manager roles"
  exclude_roles(["manager"]):
    hits = listed postings whose title matches                 # saved/applied left out
    > MAX_DISMISS -> refused, nothing changes
    save preferences (+manager), apply_preferences             # stored jobs re-filtered
    record_decision(dismissed) for each hit                    # Recycle bin
    action jobs_changed {terms, count}  -> page refetches, shows "N moved to the Recycle bin"

"find more FDE roles"
  add_intent / refresh_jobs -> request_refresh -> {started, runId}
  action refresh {started, runId}
  chat: watchRun(runId) -> status line "Searching: score 4/12"
        run ends -> refetch list -> "Found 3 new roles. 9 roles scored, 6 cleared ..."
                    -> POST /api/assistant/note
```

## Testing

| Layer | What it proves |
|---|---|
| pytest `tests/e2e/test_assistant.py` | `exclude_roles` saves the word, re-filters, bins the listed match, leaves a below-bar match unbinned, never touches a saved job, refuses empty, too many, too long, and too-broad requests with no change. A started refresh carries `runId`. |
| pytest `tests/contract/test_assistant_api.py` | A chat turn writes an `agent_run` row joined to its tool rows. `POST /api/assistant/note` stores the text and refuses empty or long text. |
| pytest `tests/e2e/test_score_and_digest.py` | `ScoreReport.capped` counts candidates the per-company cap held back. |
| vitest `assistant-run.test.tsx` | The chat shows progress, posts "Found N" or "Nothing new", a failure message, the refusal reason, follows an already-active run when queued, reloads the list once, saves the note. The summary parser and limit wording. |
| Manual (local model) | "remove manager positions": the jobs leave the list, show in the Recycle bin, and the term is in Preferences. "find FDE roles at frontier labs": progress in chat, then a result message. Settings shows a chat `agent_run` row. |

## Non-goals

Dismissing postings that are not listed, undoing an exclusion from chat (use Preferences), streaming replies, persisting the chat thread across reloads, storing curator shortlist reasons, and changing `batch_size` or `max_per_company` defaults.
