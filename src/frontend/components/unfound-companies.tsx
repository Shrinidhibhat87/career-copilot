"use client";

import { useState } from "react";

import { Disclosure } from "@/components/disclosure";
import { setCompanyBoard, type CompanyListSummary } from "@/lib/api";

type Company = CompanyListSummary["companies"][number];

const message = (error: unknown) => (error instanceof Error ? error.message : "Something went wrong.");

function UnfoundRow({ company, onResolved }: { company: Company; onResolved: () => void }) {
  const [board, setBoard] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputId = `board-${company.id}`;
  const errorId = `board-error-${company.id}`;

  async function submit() {
    if (!board.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await setCompanyBoard(company.id, board.trim());
      onResolved();
    } catch (failure) {
      setError(message(failure));
      setBusy(false);
    }
  }

  return (
    <li className="unfound-row">
      <label className="source-label" htmlFor={inputId}>
        {company.name}
      </label>
      {company.lastError ? <p className="pref-hint">{company.lastError}</p> : null}
      <form
        className="chip-add"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <input
          aria-describedby={error ? errorId : undefined}
          disabled={busy}
          id={inputId}
          onChange={(event) => setBoard(event.target.value)}
          placeholder="Careers link, or vendor/slug (ashby/n26)"
          value={board}
        />
        <button className="ghost-action" disabled={busy || !board.trim()} type="submit">
          {busy ? "Checking…" : "Check and save"}
        </button>
      </form>
      {error ? (
        <p className="form-error" id={errorId} role="alert">
          {error}
        </p>
      ) : null}
    </li>
  );
}

/** Companies the app could not match to a job board by name. The user knows where they
 * hire, so they say so here; the answer is verified against the real board before it is
 * kept, and it is never overwritten by a later sync. */
export function UnfoundCompanies({
  companies,
  onResolved,
}: {
  companies: Company[];
  onResolved: () => void;
}) {
  const unfound = companies.filter((c) => c.status === "unresolved" || c.status === "blocked");
  if (unfound.length === 0) return null;
  return (
    <Disclosure
      info="The app guesses a company's job board from its name and can miss. Paste the link to the company's careers page (or write vendor/slug, such as ashby/n26) and it is checked against the real board before it is saved."
      level="group"
      title={`${unfound.length} ${unfound.length === 1 ? "company" : "companies"} not found — tell us where they hire`}
    >
      <ul className="unfound-list">
        {unfound.map((company) => (
          <UnfoundRow company={company} key={company.id} onResolved={onResolved} />
        ))}
      </ul>
    </Disclosure>
  );
}
