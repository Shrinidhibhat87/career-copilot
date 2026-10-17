"use client";

import Link from "next/link";
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from "react";

import {
  getRuns,
  saveAssistantNote,
  sendAssistant,
  type AssistantAction,
  type AssistantTurn,
  type PipelineRun,
} from "@/lib/api";
import { describeRun, isActive, watchRun } from "@/lib/run-watch";

/** The server reads this many earlier turns; sending more only costs bytes. */
const HISTORY_TURNS = 10;

const progressLabel = (run: PipelineRun) =>
  run.total > 0
    ? `Searching: ${run.stage} ${run.completed}/${run.total}`
    : `Searching: ${run.stage || "starting"}`;

const POSITION_KEY = "copilot-position";
/** Distance from the right and bottom edges, as the button sits until it is moved. */
const HOME = { right: 24, bottom: 24 };
/** Pointer travel under which a press is still a click, not a drag. */
const DRAG_SLOP = 4;

type Position = { right: number; bottom: number };

function readPosition(): Position {
  try {
    const raw = window.localStorage.getItem(POSITION_KEY);
    if (raw) {
      const value = JSON.parse(raw) as Partial<Position>;
      if (Number.isFinite(value.right) && Number.isFinite(value.bottom)) {
        return { right: value.right as number, bottom: value.bottom as number };
      }
    }
  } catch {
    // Storage blocked or unreadable: the button stays where it starts.
  }
  return HOME;
}

const clamp = (value: number, max: number) =>
  Math.min(Math.max(value, 0), Math.max(max, 0));

/** Where the floating Copilot sits, and the handlers that let the person move it. The
 * position is the distance from the right and bottom edges, kept in this browser so a
 * reload leaves it where it was dropped. */
function useMovable(element: React.RefObject<HTMLElement | null>) {
  const [position, setPosition] = useState<Position>(HOME);
  const drag = useRef<{
    x: number;
    y: number;
    start: Position;
    moved: boolean;
  } | null>(null);
  const swallowClick = useRef(false);

  useEffect(() => {
    // After the first render, so the server render and the client agree.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPosition(readPosition());
  }, []);

  function keepOnScreen(next: Position): Position {
    const box = element.current?.getBoundingClientRect();
    const width = box?.width ?? 0;
    const height = box?.height ?? 0;
    return {
      right: clamp(next.right, window.innerWidth - width),
      bottom: clamp(next.bottom, window.innerHeight - height),
    };
  }

  useEffect(() => {
    const onResize = () => setPosition((current) => keepOnScreen(current));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handlers = {
    onPointerDown(event: ReactPointerEvent<HTMLElement>) {
      if (event.button !== 0) return;
      drag.current = {
        x: event.clientX,
        y: event.clientY,
        start: position,
        moved: false,
      };
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    onPointerMove(event: ReactPointerEvent<HTMLElement>) {
      const current = drag.current;
      if (!current) return;
      const dx = event.clientX - current.x;
      const dy = event.clientY - current.y;
      if (!current.moved && Math.hypot(dx, dy) < DRAG_SLOP) return;
      current.moved = true;
      setPosition(
        keepOnScreen({
          right: current.start.right - dx,
          bottom: current.start.bottom - dy,
        }),
      );
    },
    onPointerUp(event: ReactPointerEvent<HTMLElement>) {
      const current = drag.current;
      drag.current = null;
      event.currentTarget.releasePointerCapture?.(event.pointerId);
      if (!current?.moved) return;
      // The click that follows a drag is not a press on the button.
      swallowClick.current = true;
      window.setTimeout(() => {
        swallowClick.current = false;
      }, 0);
      try {
        window.localStorage.setItem(
          POSITION_KEY,
          JSON.stringify(keepOnScreen(position)),
        );
      } catch {
        // Not remembered this time; it still moved.
      }
    },
  };

  const style = {
    "--copilot-right": `${position.right}px`,
    "--copilot-bottom": `${position.bottom}px`,
  } as CSSProperties;

  return { style, handlers, swallowClick };
}

/** A floating "Copilot" button that opens a small chat. The agent can change the view, add
 * a preference, exclude roles or start a search; what it did arrives as actions the page
 * applies to its own state. A search it started is followed here: live progress, then a
 * message saying what was found. Enter sends, Shift+Enter adds a line. */
export function AssistantChat({
  view,
  onActions,
  knownIds,
  onRefetch,
  pollMs = 3000,
}: {
  view: { limit: number; includeBelow: boolean };
  onActions: (actions: AssistantAction[]) => void;
  /** The ids the list shows now, to count what a search adds. */
  knownIds: number[];
  /** Reloads the list and resolves with the ids it now holds. */
  onRefetch: () => Promise<number[]>;
  pollMs?: number;
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [turns, setTurns] = useState<AssistantTurn[]>([]);
  const [waiting, setWaiting] = useState(false);
  const [slow, setSlow] = useState(false);
  const [problem, setProblem] = useState("");
  const [searching, setSearching] = useState("");
  const [unread, setUnread] = useState(false);
  const alive = useRef(true);
  const watching = useRef<number | null>(null);
  const known = useRef(knownIds);
  const refetch = useRef(onRefetch);
  const opened = useRef(false);
  const list = useRef<HTMLOListElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const launcher = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLElement>(null);
  const movable = useMovable(open ? panel : launcher);

  useEffect(() => {
    known.current = knownIds;
    refetch.current = onRefetch;
    opened.current = open;
  });

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    list.current?.scrollTo?.({ top: list.current.scrollHeight });
  }, [turns, waiting, open]);

  useEffect(() => {
    if (open) input.current?.focus();
  }, [open]);

  /** A message from the page itself, not the model: what a search it started came to. */
  function say(text: string) {
    if (!alive.current) return;
    setTurns((current) => [...current, { role: "assistant", text }]);
    if (!opened.current) setUnread(true);
  }

  async function follow(runId: number, queued: boolean) {
    if (watching.current === runId) return;
    watching.current = runId;
    const before = new Set(known.current);
    setSearching("Searching…");
    try {
      const run = await watchRun(runId, {
        pollMs,
        isAlive: () => alive.current,
        onProgress: (current) => {
          if (isActive(current)) setSearching(progressLabel(current));
        },
      });
      if (run === null) return;
      let found = 0;
      if (run.state === "succeeded") {
        const ids = await refetch.current();
        found = ids.filter((id) => !before.has(id)).length;
      }
      const text = describeRun(run, found);
      say(queued ? `${text} (This was the search already running.)` : text);
      void saveAssistantNote(text).catch(() => undefined);
    } catch (failure) {
      say(failure instanceof Error ? failure.message : "The request failed.");
    } finally {
      if (watching.current === runId) watching.current = null;
      if (alive.current) setSearching("");
    }
  }

  async function followActions(actions: AssistantAction[]) {
    for (const action of actions) {
      if (action.kind !== "refresh") continue;
      if (action.started && action.runId !== undefined) {
        void follow(action.runId, false);
      } else if (!action.started && action.reason) {
        say(`No search started: ${action.reason}.`);
        if (action.reason.startsWith("queued")) {
          const active = (await getRuns().catch(() => [])).find(isActive);
          if (active) void follow(active.runId, true);
        }
      }
    }
  }

  async function send() {
    const message = text.trim();
    if (!message || waiting) return;
    const history = turns.slice(-HISTORY_TURNS);
    setWaiting(true);
    setSlow(false);
    setProblem("");
    setTurns([...turns, { role: "user", text: message }]);
    setText("");
    try {
      const reply = await sendAssistant(message, history, view);
      setTurns((current) => [
        ...current,
        { role: "assistant", text: reply.text },
      ]);
      setSlow(Boolean(reply.busy));
      onActions(reply.actions);
      void followActions(reply.actions);
    } catch (failure) {
      setProblem(
        failure instanceof Error ? failure.message : "The request failed.",
      );
    } finally {
      setWaiting(false);
      input.current?.focus();
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter keeps its usual meaning. Never while an IME is composing.
    if (event.key !== "Enter" || event.shiftKey) return;
    if (event.nativeEvent.isComposing) return;
    event.preventDefault();
    void send();
  }

  if (!open) {
    return (
      <button
        aria-label="Open Copilot chat"
        className="assistant-launcher"
        ref={launcher}
        style={movable.style}
        type="button"
        {...movable.handlers}
        onClick={() => {
          if (movable.swallowClick.current) return;
          setOpen(true);
          setUnread(false);
        }}
      >
        <span aria-hidden="true">✦</span> Copilot
        {searching ? " · searching…" : unread ? " · new message" : ""}
      </button>
    );
  }

  return (
    <section
      className="assistant-panel"
      aria-label="Copilot chat"
      ref={panel}
      style={movable.style}
    >
      <header
        className="assistant-header"
        title="Drag to move"
        onPointerDown={(event) => {
          if ((event.target as HTMLElement).closest("button")) return;
          movable.handlers.onPointerDown(event);
        }}
        onPointerMove={movable.handlers.onPointerMove}
        onPointerUp={movable.handlers.onPointerUp}
      >
        <span className="assistant-title">
          <span aria-hidden="true">✦</span> Copilot
        </span>
        <button
          aria-label="Close Copilot chat"
          className="assistant-close"
          type="button"
          onClick={() => setOpen(false)}
        >
          ×
        </button>
      </header>
      <ol className="assistant-turns" ref={list} aria-live="polite">
        {turns.length === 0 && !waiting ? (
          <li className="assistant-turn assistant assistant-hint">
            Ask me to show more jobs, add a preference like &ldquo;no
            fintech&rdquo;, or look for new jobs.
          </li>
        ) : null}
        {turns.map((turn, index) => (
          <li className={`assistant-turn ${turn.role}`} key={index}>
            {turn.text}
          </li>
        ))}
        {waiting ? (
          <li
            aria-label="Copilot is typing"
            className="assistant-turn assistant assistant-typing"
          >
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
          </li>
        ) : null}
      </ol>
      {searching ? (
        <p className="assistant-note" role="status">
          {searching}{" "}
          {/* A new tab: leaving this page unmounts the chat and loses the thread. */}
          <Link
            className="soft-link"
            href="/settings"
            rel="noopener noreferrer"
            target="_blank"
          >
            Details
          </Link>
        </p>
      ) : null}
      {slow ? (
        <p className="assistant-note">
          A search is running, replies may be slow.
        </p>
      ) : null}
      {problem ? (
        <p className="assistant-note assistant-error" role="alert">
          {problem}
        </p>
      ) : null}
      <div className="assistant-input">
        <textarea
          aria-label="Message to the assistant"
          placeholder="Message Copilot"
          ref={input}
          rows={1}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <button
          aria-label="Send"
          className="assistant-send"
          disabled={waiting || !text.trim()}
          type="button"
          onClick={() => void send()}
        >
          <svg
            aria-hidden="true"
            fill="none"
            height="16"
            stroke="currentColor"
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="2.2"
            viewBox="0 0 24 24"
            width="16"
          >
            <path d="M12 19V5M5 12l7-7 7 7" />
          </svg>
        </button>
      </div>
    </section>
  );
}
