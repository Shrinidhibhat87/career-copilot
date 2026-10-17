"use client";

import { useEffect, useState } from "react";

import { Disclosure } from "@/components/disclosure";
import {
  clearOpenRouterKey, getModelSettings, getOpenRouterModels, saveModelSettings,
  saveOpenRouterKey, SettingsError, testOpenRouterKey, type CatalogModel, type KeyStatus,
  type LiveModelRow, type ModelSettingsView, type Preset, type ResolvedModelRole,
  type RoleOverride,
} from "@/lib/api";
import { euros } from "@/lib/money";

const PRESETS: { value: Preset; label: string; hint: string }[] = [
  { value: "local", label: "Local", hint: "Every role on this machine. Free, nothing leaves it." },
  { value: "hybrid", label: "Hybrid", hint: "Triage stays local, the rest on OpenRouter." },
  { value: "cloud", label: "Cloud", hint: "Every role on OpenRouter. For machines without a GPU." },
];
const TIER_LABEL = { recommended: "Recommended", challenger: "Challengers", baseline: "Baseline" };
const OTHER = "__other__";

const dollars = (value: number | null) => (value === null ? "?" : `$${value.toFixed(2)}`);
const priceLine = (row: { priceIn: number | null; priceOut: number | null; dynamic: boolean }) =>
  row.dynamic ? "priced per call" : `${dollars(row.priceIn)} / ${dollars(row.priceOut)} per M tokens`;
const message = (error: unknown) => (error instanceof Error ? error.message : "The request failed.");
const KEY_SOURCE: Record<KeyStatus["source"], string> = {
  env: "from .env",
  keychain: "saved in the OS keychain",
  session: "kept for this session only",
  none: "",
};

/** "Local only" or "Cloud: N roles (redacted)", from the roles in force. */
export function modelPrivacy(view: ModelSettingsView): string {
  const cloud = view.roles.filter((role) => !role.local).length;
  return cloud === 0 ? "Local only" : `Cloud: ${cloud} role${cloud === 1 ? "" : "s"} (redacted)`;
}

/** What a key test found, worded so a credit total never reads as this test's cost. */
export function keyTestLine(result: { valid: boolean; limit: number | null; usage: number | null }) {
  if (!result.valid) return "OpenRouter rejected this key.";
  const spent = result.usage === null ? "" : ` This key has used ${dollars(result.usage)} of credit so far`
    + (result.limit === null ? "." : ` out of its ${dollars(result.limit)} limit.`);
  return `The key works.${spent} Testing it cost nothing.`;
}

type Pending = { preset: Preset | null; overrides: Record<string, RoleOverride> };

export function ModelSettings() {
  const [view, setView] = useState<ModelSettingsView | null>(null);
  const [preset, setPreset] = useState<Preset | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [consent, setConsent] = useState<Pending | null>(null);
  const [editing, setEditing] = useState<string | null>(null);

  useEffect(() => {
    let disposed = false;
    void getModelSettings()
      .then((next) => { if (!disposed) { setView(next); setPreset(next.settings.preset); } })
      .catch((failure) => { if (!disposed) setError(message(failure)); });
    return () => { disposed = true; };
  }, []);

  async function save(pending: Pending, cloudConsent = false) {
    setBusy(true); setError(""); setNotice("");
    try {
      const next = await saveModelSettings({ ...pending, ...(cloudConsent ? { cloudConsent } : {}) });
      setView(next); setPreset(next.settings.preset); setConsent(null); setEditing(null);
      setNotice("Saved. The next run uses these models.");
    } catch (failure) {
      if (failure instanceof SettingsError && failure.status === 428) setConsent(pending);
      else setError(message(failure));
    } finally { setBusy(false); }
  }

  if (!view) {
    return <section className="run-card" aria-label="Models">
      <Disclosure title="Models" headingId="model-setup">
        {error ? <p className="model-error" role="alert">{error}</p> : <p>Loading…</p>}
      </Disclosure>
    </section>;
  }
  const overrides = view.settings.overrides;
  const withoutOverride = (role: string) =>
    Object.fromEntries(Object.entries(overrides).filter(([name]) => name !== role));
  // Local means nothing leaves the machine, so it drops cloud overrides as well.
  const localOverrides = Object.fromEntries(
    Object.entries(overrides).filter(([, o]) => o.backend !== "openrouter"),
  );
  const cloudWanted = (preset !== null && preset !== "local") || view.roles.some((r) => !r.local);
  const changed = preset !== null && preset !== view.settings.preset;

  return <section className="run-card" aria-label="Models">
    <Disclosure
      title="Models"
      headingId="model-setup"
      forceOpen={Boolean(error || consent)}
      info="Where each step's model runs. A change applies to the next run. Cloud calls are redacted and capped by the run budget."
    >
      <p className="model-privacy"><strong>{modelPrivacy(view)}</strong> · {view.profile.reason}</p>
      {view.envProfile ? <p className="model-warning" role="note">CAREER_COPILOT_PROFILE={view.envProfile} is set in this shell, so a saved preset does not apply until it is unset.</p> : null}

      <div className="model-presets" role="radiogroup" aria-label="Preset">
        {PRESETS.map((option) => <button key={option.value} type="button" role="radio"
          aria-checked={preset === option.value} disabled={busy}
          className={`model-preset${preset === option.value ? " is-selected" : ""}`}
          onClick={() => { setPreset(option.value); setError(""); setNotice(""); }}>
          <strong>{option.label}</strong>
          <span>{option.hint}</span>
        </button>)}
      </div>
      {changed ? <div className="model-savebar model-reveal">
        <button className="primary-action" type="button" disabled={busy}
          onClick={() => void save({ preset, overrides: preset === "local" ? localOverrides : overrides })}>
          {busy ? "Saving…" : `Use ${PRESETS.find((p) => p.value === preset)?.label}`}
        </button>
        <button type="button" disabled={busy} onClick={() => { setPreset(view.settings.preset); setError(""); }}>Cancel</button>
      </div> : null}
      {error ? <p className="model-error model-reveal" role="alert">{error}</p> : null}
      {notice ? <p className="model-notice model-reveal" role="status">{notice}</p> : null}

      {consent ? <div className="run-confirmation model-reveal" role="alertdialog" aria-label="Cloud notice">
        <h3>Your CV and jobs will leave this machine</h3>
        <p>Cloud roles send the CV, your preferences and job postings to OpenRouter and the model provider behind it. Names, emails, phone numbers and addresses are replaced first; employers, dates and skills are still sent. You are asked once.</p>
        <div className="run-actions">
          <button className="primary-action" type="button" disabled={busy} onClick={() => void save(consent, true)}>Accept and save</button>
          <button type="button" disabled={busy} onClick={() => setConsent(null)}>Cancel</button>
        </div>
      </div> : null}

      {cloudWanted ? <KeyPanel status={view.key}
        onChange={(key) => setView((current) => (current ? { ...current, key } : current))} /> : null}

      <table className="model-roles">
        <thead><tr><th>Role</th><th className="model-col">Model</th><th aria-label="Actions" /></tr></thead>
        <tbody>{view.roles.map((role) => <tr key={role.role}>
          <td>{role.role}</td>
          {editing === role.role
            ? <td colSpan={2}><RoleEditor role={role} backends={view.backends} busy={busy}
              onPick={(override) => void save({ preset: view.settings.preset, overrides: { ...overrides, [role.role]: override } })}
              onCancel={() => setEditing(null)} /></td>
            : <>
              <td className="model-col">
                <span className={`model-kind model-kind-${role.local ? "local" : "cloud"}`}>{role.kind}</span>{" "}
                {role.model}
                {role.source === "override" ? <span className="privacy-tag">override</span> : null}
              </td>
              <td className="model-row-actions">
                <button type="button" disabled={busy} onClick={() => setEditing(role.role)}>Change</button>
                {role.source === "override" ? <button type="button" disabled={busy}
                  onClick={() => void save({ preset: view.settings.preset, overrides: withoutOverride(role.role) })}>Reset</button> : null}
              </td>
            </>}
        </tr>)}</tbody>
      </table>

      <p>Run cap {euros(view.budget.maxEurPerRun)}{view.budget.lastRunEur === null ? "" : `; the last run spent ${euros(view.budget.lastRunEur)}`}.</p>
    </Disclosure>
  </section>;
}

/** The OpenRouter key. Once one is in use it is never shown again: a status line, with
 * replace and remove folded into a dropdown. */
function KeyPanel({ status, onChange }: { status: KeyStatus; onChange: (status: KeyStatus) => void }) {
  const [key, setKey] = useState("");
  const [note, setNote] = useState("");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  async function run(action: "test" | "save" | "clear") {
    setBusy(true); setNote(""); setFailed(false);
    try {
      if (action === "test") {
        const result = await testOpenRouterKey(key);
        setNote(keyTestLine(result)); setFailed(!result.valid);
        return;
      }
      const next = action === "save" ? await saveOpenRouterKey(key) : await clearOpenRouterKey();
      onChange(next); setKey("");
      setNote(action === "clear" ? "Key removed."
        : next.source === "session"
          ? "Key saved and hidden. This machine has no OS keychain, so it is kept only until the app stops: add OPENROUTER_API_KEY to .env to keep it."
          : "Key saved to the OS keychain and hidden. Only its last four characters are shown.");
    } catch (failure) { setNote(message(failure)); setFailed(true); }
    finally { setBusy(false); }
  }

  const form = <div className="run-actions">
    <input type="password" aria-label="OpenRouter key" placeholder="sk-or-…" value={key} autoComplete="off"
      onChange={(event) => setKey(event.target.value)} />
    <button type="button" disabled={busy || !key} onClick={() => void run("test")}>Test</button>
    <button className="primary-action" type="button" disabled={busy || !key} onClick={() => void run("save")}>
      {busy ? "Checking…" : "Test and save"}
    </button>
  </div>;

  return <div className="model-key model-reveal">
    <h3>OpenRouter key</h3>
    {status.set ? <>
      <p className="model-key-status"><span aria-hidden="true">🔒</span> Key ending …{status.tail ?? "····"} is in use, {KEY_SOURCE[status.source]}.</p>
      {status.source === "env" ? null : <details className="model-key-menu">
        <summary>Manage key</summary>
        <div className="model-reveal">
          {form}
          <button type="button" disabled={busy} onClick={() => void run("clear")}>Remove key</button>
        </div>
      </details>}
    </> : <>
      <p>Needed for any role on OpenRouter. It is tested before it is saved, then hidden.</p>
      {form}
    </>}
    {note ? <p className={failed ? "model-error" : "model-notice"} role="status">{note}</p> : null}
  </div>;
}

/** Inline, in the role's own row: the catalog by tier, then any OpenRouter model that can do
 * the role's job, or a local backend by name. */
function RoleEditor({ role, backends, busy, onPick, onCancel }: {
  role: ResolvedModelRole;
  backends: string[];
  busy: boolean;
  onPick: (override: RoleOverride) => void;
  onCancel: () => void;
}) {
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [live, setLive] = useState<LiveModelRow[]>([]);
  const [liveError, setLiveError] = useState("");
  const [choice, setChoice] = useState("");
  const [other, setOther] = useState("");
  const [backend, setBackend] = useState("openrouter");
  const [reserve, setReserve] = useState("");

  useEffect(() => {
    let disposed = false;
    void getOpenRouterModels(role.role).then((body) => {
      if (disposed) return;
      setCatalog(body.catalog); setLive(body.models); setLiveError(body.liveError);
      setChoice(body.catalog[0]?.id ?? OTHER);
    }).catch((failure) => { if (!disposed) { setLiveError(message(failure)); setChoice(OTHER); } });
    return () => { disposed = true; };
  }, [role.role]);

  const picked = catalog.find((row) => row.id === choice);
  const dynamic = choice === OTHER && (live.find((row) => row.id === other)?.dynamic ?? false);
  const reserveEur = reserve ? Number(reserve) : null;
  const ready = choice === OTHER ? Boolean(other.trim()) : Boolean(picked);

  function apply() {
    if (choice !== OTHER) onPick({ backend: "openrouter", model: choice });
    else onPick({ backend, model: other.trim(), ...(reserveEur ? { reserveEur } : {}) });
  }

  return <div className="model-editor model-reveal">
    <select aria-label={`Model for ${role.role}`} value={choice} disabled={busy}
      onChange={(event) => setChoice(event.target.value)}>
      {(["recommended", "challenger", "baseline"] as const).map((tier) => {
        const rows = catalog.filter((row) => row.tier === tier);
        return rows.length ? <optgroup key={tier} label={TIER_LABEL[tier]}>
          {rows.map((row) => <option key={row.id} value={row.id} disabled={row.available === false}>
            {row.id} · {row.available === false ? "no longer on OpenRouter" : priceLine(row)}
          </option>)}
        </optgroup> : null;
      })}
      <option value={OTHER}>Another model…</option>
    </select>
    {picked ? <p className="model-editor-note">{picked.note}</p> : null}
    {choice === OTHER ? <div className="model-editor-other model-reveal">
      <select aria-label="Backend" value={backend} onChange={(event) => setBackend(event.target.value)}>
        {backends.map((name) => <option key={name} value={name}>{name}</option>)}
      </select>
      <input aria-label="Any model" list={`models-${role.role}`} placeholder="model id, e.g. qwen/qwen3-32b"
        value={other} onChange={(event) => setOther(event.target.value)} />
      <datalist id={`models-${role.role}`}>{live.map((row) => <option key={row.id} value={row.id}>{priceLine(row)}</option>)}</datalist>
      {dynamic ? <input aria-label="Reserve per call (€)" type="number" min="0.001" step="0.001"
        placeholder="reserve €/call" value={reserve} onChange={(event) => setReserve(event.target.value)} /> : null}
    </div> : null}
    {liveError ? <p className="model-editor-note">Live prices unavailable: {liveError}</p> : null}
    <div className="model-row-actions">
      <button className="primary-action" type="button" disabled={busy || !ready} onClick={apply}>Use</button>
      <button type="button" disabled={busy} onClick={onCancel}>Cancel</button>
    </div>
  </div>;
}
