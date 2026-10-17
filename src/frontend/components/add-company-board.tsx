"use client";

import { useState } from "react";

import { AddedCompanies } from "@/components/added-companies";
import {
  confirmBoard,
  lookupCompany,
  searchCompanyOnWeb,
  type BoardLookup,
  type FoundBoard,
} from "@/lib/api";

const message = (error: unknown) =>
  error instanceof Error ? error.message : "Something went wrong.";

/** Type one company, see the job board it is on, and add it. Nothing is crawled until
 * the user confirms, and the server checks the board again before adding it. */
export function AddCompanyBoard({ onAdded }: { onAdded?: () => void }) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [lookup, setLookup] = useState<BoardLookup | null>(null);
  const [webBoards, setWebBoards] = useState<FoundBoard[] | null>(null);
  const [added, setAdded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run<T>(work: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      return await work();
    } catch (failure) {
      setError(message(failure));
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  async function find() {
    const typed = name.trim();
    if (!typed || busy) return;
    setAdded(null);
    setWebBoards(null);
    const result = await run(() => lookupCompany(typed));
    if (result) setLookup(result);
  }

  async function searchWeb() {
    if (!lookup) return;
    const result = await run(() => searchCompanyOnWeb(lookup.name));
    if (result) setWebBoards(result.boards);
  }

  async function add(boardType: string, slug: string) {
    const result = await run(() => confirmBoard(boardType, slug, lookup?.name));
    if (!result) return;
    const board = `${result.boardType}/${result.slug}`;
    if (result.alreadyListed) {
      setAdded(`${board} is already in your list. Nothing was changed.`);
    } else {
      onAdded?.();
      setAdded(
        result.filledListEntry
          ? `✓ Added ${board} for ${result.filledListEntry}, which was not found before. It is included from the next run.`
          : `✓ Added ${board}. It is included from the next run.`,
      );
    }
    setLookup(null);
    setWebBoards(null);
    setName("");
  }

  return (
    <section className="pref-section" aria-labelledby="add-company-heading">
      <div className="pref-heading-row">
        <h2 id="add-company-heading">Add a company</h2>
        <AddedCompanies />
      </div>
      <p className="pref-hint">
        Type a company name. We look for its job board on the eight platforms we read, and add it
        only after you confirm.
      </p>
      <form
        className="chip-add"
        onSubmit={(event) => {
          event.preventDefault();
          void find();
        }}
      >
        <input
          aria-label="Company name"
          disabled={busy}
          onChange={(event) => setName(event.target.value)}
          placeholder="Company name, for example Helsing"
          value={name}
        />
        <button className="ghost-action" disabled={busy || !name.trim()} type="submit">
          {busy ? "Looking…" : "Find job board"}
        </button>
      </form>

      {added ? (
        <p className="source-detected" role="status">
          {added}
        </p>
      ) : null}

      {lookup?.found && lookup.boardType && lookup.slug ? (
        <div className="unfound-row" role="status">
          <p>
            Found <strong>{lookup.boardType}/{lookup.slug}</strong> with {lookup.openRoles ?? 0} open
            role(s).
          </p>
          <button
            className="add-board-action"
            disabled={busy}
            onClick={() => void add(lookup.boardType!, lookup.slug!)}
            type="button"
          >
            Add this board
          </button>
        </div>
      ) : null}

      {lookup && !lookup.found ? (
        <div className="unfound-row" role="status">
          {lookup.blockedReason ? (
            <p className="pref-hint">
              Could not check the boards right now ({lookup.blockedReason}). Try again in a minute.
            </p>
          ) : (
            <p className="pref-hint">
              No public board found for {lookup.name} (tried {lookup.tried.slice(0, 6).join(", ")}).
            </p>
          )}
          {lookup.canSearchWeb && webBoards === null ? (
            <button
              className="ghost-action"
              disabled={busy}
              onClick={() => void searchWeb()}
              type="button"
            >
              Search the web for it
            </button>
          ) : null}
        </div>
      ) : null}

      {webBoards !== null ? (
        webBoards.length === 0 ? (
          <p className="pref-hint" role="status">
            The web search did not find a board for {lookup?.name}.
          </p>
        ) : (
          <ul className="unfound-list">
            {webBoards.map((board) => (
              <li className="unfound-row" key={`${board.boardType}/${board.slug}`}>
                <p>
                  <strong>
                    {board.boardType}/{board.slug}
                  </strong>{" "}
                  — {board.openRoles ?? 0} open role(s)
                </p>
                {board.sampleTitles?.length ? (
                  <p className="pref-hint">e.g. {board.sampleTitles.join("; ")}</p>
                ) : null}
                <button
                  className="add-board-action"
                  disabled={busy}
                  onClick={() => void add(board.boardType, board.slug)}
                  type="button"
                >
                  Add this board
                </button>
              </li>
            ))}
          </ul>
        )
      ) : null}

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
    </section>
  );
}
