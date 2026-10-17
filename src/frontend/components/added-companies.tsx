"use client";

import { useState } from "react";

import { Modal } from "@/components/modal";
import { getBoardVisits, type BoardVisit } from "@/lib/api";

const ORIGINS: Record<BoardVisit["origin"], string> = {
  imported: "Company list",
  manual: "Added by you",
  approved: "Agent-found",
  sample_role: "Sample role",
  config: "Config",
};

const STATUSES: Record<BoardVisit["status"], string> = {
  resolved: "Board found",
  unresolved: "Not found",
  blocked: "Could not be checked",
  inactive: "Inactive",
};

const date = (iso: string | null, empty: string) =>
  iso
    ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
    : empty;

/** A button that opens a read-only table of every board the crawler visits, the one the
 * next run reaches first at the top. Loaded on open, so it is never stale. */
export function AddedCompanies() {
  const [boards, setBoards] = useState<BoardVisit[] | null>(null);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function show() {
    setOpen(true);
    setBoards(null);
    setError(null);
    try {
      setBoards(await getBoardVisits());
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "Something went wrong.");
    }
  }

  return (
    <>
      <button className="ghost-action" onClick={() => void show()} type="button">
        View added companies
      </button>
      {open ? (
        <Modal label="Added companies" onClose={() => setOpen(false)}>
          <div className="modal-header">
            <h2>Companies we visit</h2>
            <button className="ghost-action" onClick={() => setOpen(false)} type="button">
              Close
            </button>
          </div>
          <p className="pref-hint">
            In the order the next run reaches them: boards never visited first, then the one that
            has waited longest.
          </p>
          {error ? (
            <p className="form-error" role="alert">
              {error}
            </p>
          ) : boards === null ? (
            <p aria-live="polite" className="live-status">
              Loading…
            </p>
          ) : (
            <div className="board-table-wrap">
              <table className="board-table">
                <thead>
                  <tr>
                    <th scope="col">Company</th>
                    <th scope="col">Board</th>
                    <th scope="col">Status</th>
                    <th scope="col">Source</th>
                    <th scope="col">Added</th>
                    <th scope="col">Open roles</th>
                    <th scope="col">Last visited</th>
                  </tr>
                </thead>
                <tbody>
                  {boards.map((board) => (
                    <tr key={`${board.boardType}/${board.slug}`}>
                      <th scope="row">{board.name}</th>
                      <td>
                        {board.boardType}/{board.slug}
                      </td>
                      <td>{STATUSES[board.status]}</td>
                      <td>{ORIGINS[board.origin]}</td>
                      <td>{date(board.addedAt, "—")}</td>
                      <td>{board.openRoles ?? "—"}</td>
                      <td>{date(board.lastVisitedAt, "Never")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Modal>
      ) : null}
    </>
  );
}
