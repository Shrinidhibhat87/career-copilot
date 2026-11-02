"use client";

import { useEffect, useState } from "react";

import { Disclosure } from "@/components/disclosure";
import { getMarkets, saveMarkets, type MarketsView } from "@/lib/api";

const message = (failure: unknown) =>
  failure instanceof Error ? failure.message : "Something went wrong.";

/** The countries searched, one switch each. Only a market with a source behind it can be
 * switched on; the rest are listed as coming soon. Until the choice is confirmed the
 * default (Germany) applies, and the suggestion from the saved cities is shown beside it.
 *
 * Saves on its own, apart from the preferences form below: a market changes which sources
 * a run asks and re-checks the stored postings straight away. */
export function MarketsSection() {
  const [view, setView] = useState<MarketsView | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let current = true;
    getMarkets()
      .then((loaded) => {
        if (!current) return;
        setView(loaded);
        setChosen(loaded.enabled);
      })
      .catch((reason: unknown) => current && setError(message(reason)));
    return () => {
      current = false;
    };
  }, []);

  const toggle = (code: string) => {
    setSaved(false);
    setChosen((now) => (now.includes(code) ? now.filter((c) => c !== code) : [...now, code]));
  };

  async function save() {
    setSaving(true);
    setError("");
    try {
      const next = await saveMarkets(chosen);
      setView(next);
      setChosen(next.enabled);
      setSaved(true);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setSaving(false);
    }
  }

  const name = (code: string) => view?.packs.find((pack) => pack.code === code)?.name ?? code;

  return (
    <section className="run-card markets" aria-label="Markets">
      <Disclosure
        defaultOpen
        forceOpen={error !== ""}
        info="A market is a country you search in. It decides which free national job boards are asked and which country names count as part of a place. Fully remote jobs stay open in every market."
        title="Markets"
      >
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        {view ? (
          <>
            <div className="market-chips" role="group" aria-label="Markets">
              {view.packs.map((pack) => (
                <label
                  className="market-chip"
                  data-soon={!pack.ready}
                  key={pack.code}
                >
                  <input
                    checked={chosen.includes(pack.code)}
                    disabled={!pack.ready}
                    onChange={() => toggle(pack.code)}
                    type="checkbox"
                  />
                  {pack.name}
                  {pack.ready ? null : <small> coming soon</small>}
                </label>
              ))}
            </div>
            {view.confirmed ? null : (
              <p className="market-note">
                Not confirmed yet, so {view.enabled.map(name).join(", ")} is used by default.
                {view.proposal.length > 0
                  ? ` Your saved cities suggest ${view.proposal.map(name).join(", ")}.`
                  : ""}
              </p>
            )}
            <div className="market-actions">
              <button
                disabled={saving || chosen.length === 0}
                onClick={() => void save()}
                type="button"
              >
                {saving ? "Saving…" : view.confirmed ? "Save markets" : "Confirm markets"}
              </button>
              {saved ? <small role="status">Saved. Stored jobs were re-checked.</small> : null}
            </div>
          </>
        ) : error ? null : (
          <p>Loading…</p>
        )}
      </Disclosure>
    </section>
  );
}
