"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { CompanySources } from "@/components/company-sources";
import { PrivacyBadge } from "@/components/privacy-badge";
import {
  deleteResume,
  getResume,
  syncCompanyList,
  UNREACHABLE,
  uploadResume,
  type ResumeDetections,
} from "@/lib/api";
import { rememberSyncRun } from "@/lib/sync-run";

// `loading` and `failed` are not `empty`: the dropzone claims there is no CV, which only a
// 404 may say.
type Status = "loading" | "failed" | "empty" | "uploading" | "parsed";

export function ResumeUpload() {
  const router = useRouter();
  const [source, setSource] = useState<"notion" | "csv" | null>(null);
  const [continuing, setContinuing] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>("loading");
  const [detections, setDetections] = useState<ResumeDetections | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const fileNameRef = useRef<HTMLParagraphElement>(null);
  // The mount read is slower than a fast upload often enough to matter: once the
  // user has acted, whatever it returns is already stale and must not be applied.
  const hasActed = useRef(false);

  useEffect(() => {
    void getResume()
      .then((existing) => {
        if (hasActed.current) return;
        if (!existing) {
          setStatus("empty");
          return;
        }
        setDetections(existing);
        setStatus("parsed");
      })
      .catch(() => {
        if (hasActed.current) return;
        setStatus("failed");
        setError(UNREACHABLE);
      });
  }, []);

  useEffect(() => {
    if (status === "parsed") fileNameRef.current?.focus();
  }, [status]);

  async function submit(file: File | undefined) {
    if (!file || status === "uploading") return;
    hasActed.current = true;
    setStatus("uploading");
    setError(null);
    try {
      setDetections(await uploadResume(file));
      setStatus("parsed");
    } catch (failure) {
      // A failure never discards an already-parsed result.
      setError(
        failure instanceof Error ? failure.message : "Something went wrong.",
      );
      setStatus(detections ? "parsed" : "empty");
    } finally {
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function remove() {
    hasActed.current = true;
    try {
      await deleteResume();
    } catch {
      // The server still holds the resume, so the screen must keep showing it.
      setError(UNREACHABLE);
      return;
    }
    setDetections(null);
    setError(null);
    setStatus("empty");
  }

  // The sync belongs to this step's Continue, not to picking a file: choosing a CSV or
  // saving a Notion id only records the connection. The sync starts here, then the
  // preferences page follows it by run id.
  async function continueToPreferences() {
    setContinuing(true);
    setSyncError(null);
    if (source) {
      try {
        const started = await syncCompanyList();
        rememberSyncRun(started.runId);
      } catch (failure) {
        setSyncError(
          failure instanceof Error
            ? failure.message
            : "Could not start the sync.",
        );
        setContinuing(false);
        return;
      }
    }
    router.push("/preferences");
  }

  const summary = detections?.summary;

  return (
    <>
      <p className="live-status" aria-live="polite">
        {status === "uploading"
          ? "Parsing your resume."
          : error
            ? error
            : detections
              ? detections.changed
                ? `Stored ${detections.filename} as version ${detections.version}.`
                : `Parsed ${detections.filename}.`
              : ""}
      </p>

      {/* Shared by both states: a removal can fail while a resume is on screen. */}
      {error ? (
        <p className="form-error" id="resume-error" role="alert">
          {error}
        </p>
      ) : null}

      {status === "parsed" && detections && summary ? (
        <>
          <div className="file-card">
            <span className="file-mark" aria-hidden="true" />
            <div>
              <p className="file-name" ref={fileNameRef} tabIndex={-1}>
                {detections.filename}
              </p>
              <p className="file-summary">
                version {detections.version} · parsed · {summary.roles} roles ·{" "}
                {summary.skills} skills · {summary.languages} languages detected
              </p>
            </div>
            <button
              className="ghost-action"
              type="button"
              onClick={() => void remove()}
            >
              REMOVE
            </button>
          </div>
          <PrivacyBadge privacy={detections.privacy} />
          <p className="chips-label">
            DETECTED — CARRIED INTO YOUR PREFERENCES
          </p>
          <ul className="chips">
            {detections.detected.skills.map((skill) => (
              <li className="chip" key={skill}>
                {skill}
              </li>
            ))}
          </ul>
          <CompanySources onConfiguredChange={setSource} />
          {syncError ? (
            <p className="form-error" role="alert">
              {syncError}{" "}
              <Link href="/preferences">Continue without syncing</Link>
            </p>
          ) : null}
          <button
            className="primary-action"
            disabled={continuing}
            onClick={() => void continueToPreferences()}
            type="button"
          >
            {continuing
              ? `Syncing your ${source === "csv" ? "CSV" : "Notion"} list…`
              : "Review preferences →"}
          </button>
        </>
      ) : status === "loading" ? (
        <div className="file-card skeleton" data-testid="resume-skeleton" aria-hidden="true">
          <span className="skeleton-line" />
          <span className="skeleton-line skeleton-short" />
        </div>
      ) : status === "failed" ? null : (
        <>
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
              void submit(event.dataTransfer.files[0]);
            }}
          >
            {/* A real labelled file input: drag-and-drop is not keyboard-operable, so
                the button alone must be enough to finish the step. */}
            <label className="dropzone-heading" htmlFor="resume-file">
              Drop a PDF or DOCX here
            </label>
            {/* The file does leave the browser: it is POSTed to the local API, which parses
                it in memory. What it does not do is reach a model provider unredacted. */}
            <p className="dropzone-meta">
              max 5 MB · read by the local API on this machine · redacted before
              any model call
            </p>
            <input
              accept=".pdf,.docx"
              aria-describedby={error ? "resume-error" : undefined}
              className="file-input"
              disabled={status === "uploading"}
              id="resume-file"
              onChange={(event) => void submit(event.target.files?.[0])}
              ref={inputRef}
              type="file"
            />
            <button
              className="primary-action"
              disabled={status === "uploading"}
              onClick={() => inputRef.current?.click()}
              type="button"
            >
              {status === "uploading" ? "Parsing…" : "Choose a file"}
            </button>
          </div>
          <p className="escape-hatch">
            No resume yet?{" "}
            <Link href="/preferences">Set preferences manually</Link> — matches
            will then have no evidence to quote.
          </p>
        </>
      )}
    </>
  );
}
