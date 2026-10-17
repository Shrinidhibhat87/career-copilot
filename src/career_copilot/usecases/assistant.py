"""One chat turn with the feedback agent: save, redact, ask, save the answer.

The user's message is saved before the model is called, so a failed call never loses it.
The agent bypasses `RedactingLLMClient`, so the gateway runs here, over everything it will
read. The CV is never part of that.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.run import AgentRunResult

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.assistant.agent import AssistantLimits, usage_limits
from career_copilot.agents.assistant.tools import Action, AssistantDeps, Refresh, ViewState
from career_copilot.agents.recording import AgentRecording
from career_copilot.config import Config
from career_copilot.usecases.condense_intent import Redact

FAILURE_TEXT = "I could not answer just now. Your note is saved."
NO_ACTION_TEXT = (
    'I did not change anything. Tell me what to do, for example "exclude manager roles", '
    '"show 20" or "find remote FDE roles".'
)
RETRY_NUDGE = (
    "\n\nYou called no tool. If the user asked for a change, call the matching tool now. "
    "If not, answer without saying that anything changed."
)
# A reply that says a change happened. With no tool call behind it, it is not true: a small
# model readily describes an action instead of taking it.
CLAIM = re.compile(
    r"\b(i've|i have|i'll|i will|has been|have been|now (show|display)|will now)\b", re.I
)
HISTORY_TURNS = 10
CONTEXT_ROWS = 20


@dataclass(frozen=True, slots=True)
class Turn:
    role: str
    text: str


class AssistantReply(BaseModel):
    text: str
    actions: list[Action]
    # A search was running, so the model was shared and the reply may have been slow.
    busy: bool = False


@dataclass(frozen=True, slots=True)
class AssistantContext:
    """What the agent may read besides the chat: preferences and earlier feedback."""

    intent: list[str]
    feedback: list[str]
    corrections: list[str]

    @property
    def text(self) -> str:
        def block(title: str, lines: list[str]) -> str:
            body = "\n".join(f"- {line}" for line in lines) or "(none)"
            return f"{title}\n{body}"

        return "\n\n".join(
            [
                block("WHAT THEY ARE LOOKING FOR", self.intent),
                block("EARLIER FEEDBACK", self.feedback),
                block("THEIR CORRECTIONS TO EVIDENCE", self.corrections),
            ]
        )

    def flat(self) -> list[str]:
        return [*self.intent, *self.feedback, *self.corrections]

    def rebuilt(self, texts: Sequence[str]) -> AssistantContext:
        a, b = len(self.intent), len(self.intent) + len(self.feedback)
        return AssistantContext(list(texts[:a]), list(texts[a:b]), list(texts[b:]))


def read_context(store: SqliteStore) -> AssistantContext:
    batch = store.recent_corrections(CONTEXT_ROWS, ("batch",))
    return AssistantContext(
        intent=[e.text for e in store.active_intent()],
        # The old box's batch notes read as earlier feedback, before the chat's own rows.
        feedback=[note for _, _, note in batch]
        # A reply with no recorded action claimed nothing a tool backed up, and a model
        # copies what it reads: only replies that did something go back into the prompt.
        + [
            f"{e.role}: {e.text}"
            for e in store.recent_feedback(CONTEXT_ROWS)
            if e.role == "user" or e.actions is not None
        ],
        corrections=[
            f"{target}: {note}" if target else note
            for _, target, note in store.recent_corrections(CONTEXT_ROWS, ("evidence",))
        ],
    )


def assistant_context(store: SqliteStore, redact: Redact | None) -> AssistantContext:
    """The context as the model sees it: redacted when the gateway is on."""
    context = read_context(store)
    if redact is None:
        return context
    return context.rebuilt(redact(context.flat()).texts)


def _tool_calls(result: AgentRunResult[str]) -> int:
    usage = result.usage  # a property in pydantic-ai 2.x, a method before
    return (usage() if callable(usage) else usage).tool_calls


def _prompt(
    context: AssistantContext, history: Sequence[Turn], message: str, view: ViewState
) -> str:
    chat = "\n".join(f"{turn.role}: {turn.text}" for turn in history) or "(first message)"
    return (
        f"{context.text}\n\nTHE PAGE NOW SHOWS up to {view.limit} jobs"
        f"{', including lower matches' if view.include_below else ''}. "
        f'"N more" means set_view with limit {view.limit} + N, not the count of good matches.\n\n'
        f"CHAT SO FAR\n{chat}\n\nNEW MESSAGE FROM THE USER\n{message}"
    )


def converse(
    config: Config,
    agent_factory: Callable[[], Agent[AssistantDeps, str]],
    message: str,
    history: Sequence[Turn],
    view: ViewState,
    *,
    redact: Redact | None,
    refresh: Refresh,
    now: Callable[[], datetime],
    recording: AgentRecording | None = None,
    limits: AssistantLimits | None = None,
) -> AssistantReply:
    """Answer one message. Never raises for a model failure: it answers `FAILURE_TEXT`."""
    limits = limits or AssistantLimits()
    recent = list(history)[-HISTORY_TURNS:]
    with SqliteStore(config.db_path) as store:
        context = read_context(store)
        busy = store.has_active_run()
        store.add_feedback("user", message, None, now())
    deps = AssistantDeps(config=config, view=view, refresh=refresh, now=now)
    try:
        redaction = None
        if redact is not None:
            texts = [*context.flat(), *(t.text for t in recent), message]
            redaction = redact(texts)
            cleaned = list(redaction.texts)
            context = context.rebuilt(cleaned[: len(context.flat())])
            tail = cleaned[len(context.flat()) :]
            recent = [Turn(t.role, c) for t, c in zip(recent, tail, strict=False)]
            message = tail[-1]
        if recording is not None:
            recording.start()
        prompt = _prompt(context, recent, message, view)

        async def ask() -> AgentRunResult[str]:
            agent = agent_factory()
            result = await agent.run(prompt, deps=deps, usage_limits=usage_limits(limits))
            if _tool_calls(result) == 0 and CLAIM.search(result.output):
                result = await agent.run(
                    prompt + RETRY_NUDGE, deps=deps, usage_limits=usage_limits(limits)
                )
            return result

        result = asyncio.run(
            asyncio.wait_for(ask(), timeout=limits.busy_seconds if busy else limits.max_seconds)
        )
        if recording is not None:
            recording.finish(result)
        text = str(redaction.restore(result.output)) if redaction else result.output
        if _tool_calls(result) == 0 and CLAIM.search(text):
            text = NO_ACTION_TEXT
        reply = AssistantReply(text=text.strip() or FAILURE_TEXT, actions=deps.actions, busy=busy)
    except Exception as exc:
        # Deliberately broad: a refused connection, a limit and a timeout all mean the same
        # thing to the user, and nothing the tools already did is undone.
        if recording is not None:
            recording.finish(None, "timeout" if isinstance(exc, TimeoutError) else "error")
        reply = AssistantReply(text=FAILURE_TEXT, actions=deps.actions, busy=busy)
    with SqliteStore(config.db_path) as store:
        store.add_feedback(
            "assistant",
            reply.text,
            json.dumps([a.model_dump(by_alias=True, exclude_none=True) for a in reply.actions])
            if reply.actions
            else None,
            now(),
        )
    return reply
