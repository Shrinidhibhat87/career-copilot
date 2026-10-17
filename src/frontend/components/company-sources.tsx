"use client";

import { useEffect, useRef, useState } from "react";

import { Disclosure } from "@/components/disclosure";
import { InfoTip } from "@/components/info-tip";
import {
  getCompanyListConfig,
  saveCompanyListConfig,
  uploadCompanyCsv,
  type CompanyListConfig,
} from "@/lib/api";

type CsvStatus = "empty" | "uploading" | "uploaded";

const message = (error: unknown) => (error instanceof Error ? error.message : "Something went wrong.");

/** Where the user's target companies come from: a Notion database or a CSV. Connecting
 * only saves the connection; the sync itself starts when the user continues to the
 * preferences step (see `resume-upload.tsx`). No LLM, no search spend, no approval step —
 * importing a list the user already curated is the approval. */
export function CompanySources({
  onConfiguredChange,
}: {
  onConfiguredChange?: (configured: "notion" | "csv" | null) => void;
}) {
  const [config, setConfig] = useState<CompanyListConfig | null>(null);
  const [databaseId, setDatabaseId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [csvStatus, setCsvStatus] = useState<CsvStatus>("empty");
  const [csvError, setCsvError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function apply(next: CompanyListConfig) {
    setConfig(next);
    setDatabaseId(next.notion.databaseId);
    setCsvStatus(next.csv.path ? "uploaded" : "empty");
    onConfiguredChange?.(next.configured ?? null);
  }

  useEffect(() => {
    let disposed = false;
    void getCompanyListConfig()
      .then((loaded) => {
        if (!disposed) apply(loaded);
      })
      .catch((failure: unknown) => {
        if (!disposed) setError(message(failure));
      });
    return () => {
      disposed = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function saveNotionDatabase() {
    if (!config) return;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const saved = await saveCompanyListConfig(databaseId.trim(), config.csv.path);
      apply(saved);
      setNotice(saved.notion.databaseId ? "Saved." : "Cleared.");
    } catch (failure) {
      setError(message(failure));
    } finally {
      setSaving(false);
    }
  }

  async function submitCsv(file: File | undefined) {
    if (!file || csvStatus === "uploading") return;
    setCsvStatus("uploading");
    setCsvError(null);
    try {
      const result = await uploadCompanyCsv(file);
      apply(await getCompanyListConfig());
      setNotice(`Uploaded ${result.companies} compan${result.companies === 1 ? "y" : "ies"}.`);
    } catch (failure) {
      setCsvError(message(failure));
      setCsvStatus(config?.csv.path ? "uploaded" : "empty");
    } finally {
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  if (error && !config) {
    return (
      <section className="pref-section" aria-labelledby="company-sources-heading">
        <h2 id="company-sources-heading">Companies you are targeting</h2>
        <p className="form-error" role="alert">
          {error}
        </p>
      </section>
    );
  }
  if (!config) {
    return (
      <section className="pref-section" aria-labelledby="company-sources-heading">
        <h2 id="company-sources-heading">Companies you are targeting</h2>
        <p className="pref-hint">Loading…</p>
      </section>
    );
  }

  const notionDetected = Boolean(config.notion.databaseId);
  const csvDetected = Boolean(config.csv.path);

  return (
    <section className="pref-section" aria-labelledby="company-sources-heading">
      <div className="pref-heading-row">
        <h2 id="company-sources-heading">Companies you are targeting</h2>
        <InfoTip label="About company sources">
          Optional. Connect a Notion database or upload a CSV of companies you already follow.
          Each one is matched to its public job board and its open roles are searched — on this
          machine, with no model call.
        </InfoTip>
      </div>

      <div className="source-label-row">
        <label className="source-label" htmlFor="notion-database-id">
          Notion database ID
        </label>
        <InfoTip label="About the Notion connection">
          The ID of the Notion database that holds your company list (the long code in its URL).
          The Notion token lives in .env on this machine and is never sent to this page. Share the
          database with your integration in Notion (··· menu → Connections) so it can be read.
        </InfoTip>
        {notionDetected ? (
          <span className="source-detected">✓ Detected — included when you continue</span>
        ) : null}
      </div>
      <div className="chip-add">
        <input
          id="notion-database-id"
          onChange={(event) => setDatabaseId(event.target.value)}
          placeholder="Notion database ID"
          value={databaseId}
        />
        <button
          className="ghost-action"
          disabled={saving}
          onClick={() => void saveNotionDatabase()}
          type="button"
        >
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
      {!config.notionKeyConfigured ? (
        <p className="form-error">Add NOTION_API_KEY to .env to use Notion.</p>
      ) : null}

      <Disclosure
        forceOpen={Boolean(csvError)}
        info="A two-column name,url export. Use it when the list does not live in Notion, or to add companies Notion does not hold."
        level="group"
        title="Upload a CSV list instead"
      >
        <div className="source-label-row">
          <span className="source-label">CSV list</span>
          {csvDetected ? (
            <span className="source-detected">✓ Detected — included when you continue</span>
          ) : null}
        </div>
        <div
          className={`dropzone${isDragging ? " dropzone-active" : ""}`}
          onDragOver={(event) => {
            event.preventDefault();
            setIsDragging(true);
          }}
          onDragLeave={() => setIsDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setIsDragging(false);
            void submitCsv(event.dataTransfer.files[0]);
          }}
        >
          <label className="dropzone-heading" htmlFor="company-list-csv">
            Drop a company-list CSV here
          </label>
          <p className="dropzone-meta">max 5 MB · columns `name`, optional `url`</p>
          <input
            accept=".csv"
            aria-describedby={csvError ? "company-csv-error" : undefined}
            className="file-input"
            disabled={csvStatus === "uploading"}
            id="company-list-csv"
            onChange={(event) => void submitCsv(event.target.files?.[0])}
            ref={inputRef}
            type="file"
          />
          <button
            className="primary-action"
            disabled={csvStatus === "uploading"}
            onClick={() => inputRef.current?.click()}
            type="button"
          >
            {csvStatus === "uploading" ? "Uploading…" : "Choose a file"}
          </button>
        </div>
        {csvError ? (
          <p className="form-error" id="company-csv-error" role="alert">
            {csvError}
          </p>
        ) : null}
      </Disclosure>

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      {notice ? <p className="live-status">{notice}</p> : null}
    </section>
  );
}
