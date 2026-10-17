"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { AddCompanyBoard } from "@/components/add-company-board";
import { ChipInput } from "@/components/chip-input";
import { CompanySummary } from "@/components/company-summary";
import { Disclosure } from "@/components/disclosure";
import { InfoTip } from "@/components/info-tip";
import { IntentLog } from "@/components/intent-log";
import { NotesExtraction } from "@/components/notes-extraction";
import { SampleRoles } from "@/components/sample-roles";
import {
  getPreferences,
  previewPreferences,
  savePreferences,
  ValidationError,
  type FieldChange,
  type PreferenceLanguage,
  type Preferences,
  type PreferencesPreview,
  type SaveResult,
  type Term,
} from "@/lib/api";

// Server field name → the key that holds it here. An accepted proposal names the server's
// field, and this is the one place the two vocabularies meet.
const TERM_FIELDS: Record<string, keyof Preferences> = {
  role_families: "roleFamilies",
  title_terms: "titleTerms",
  core_skills: "coreSkills",
  excluded_stack: "excludedStack",
  excluded_role_terms: "excludedRoleTerms",
  allowed_cities: "allowedCities",
  preferred_cities: "preferredCities",
  flagged_cities: "flaggedCities",
};
const SCALAR_FIELDS: Record<string, keyof Preferences> = {
  allow_remote: "allowRemote",
  require_permanent: "requirePermanent",
  max_per_company: "maxPerCompany",
  batch_size: "batchSize",
};

const MAX_ROLE_FAMILIES = 5;

function applyChange(prefs: Preferences, change: FieldChange): Preferences {
  const termKey = TERM_FIELDS[change.field];
  if (termKey) {
    const values = prefs[termKey] as Term[];
    if (change.action === "remove") {
      return { ...prefs, [termKey]: values.filter((value) => value.term !== change.value) };
    }
    if (values.some((value) => value.term === change.value)) return prefs;
    return {
      ...prefs,
      [termKey]: [...values, { term: change.value, label: change.label, fromResume: false }],
    };
  }
  const scalarKey = SCALAR_FIELDS[change.field];
  if (scalarKey) {
    const raw = change.value.trim();
    const value = scalarKey === "maxPerCompany" || scalarKey === "batchSize"
      ? Number(raw)
      : raw.toLowerCase() === "true";
    return { ...prefs, [scalarKey]: value };
  }
  if (change.field === "seniority") {
    const bands = prefs.seniority.filter((band) => band !== change.value);
    return { ...prefs, seniority: change.action === "remove" ? bands : [...bands, change.value] };
  }
  if (change.field === "languages") {
    const [name, level] = change.value.split(":");
    const others = prefs.languages.filter(
      (language) => language.name.toLowerCase() !== name.trim().toLowerCase(),
    );
    if (change.action === "remove") return { ...prefs, languages: others };
    return {
      ...prefs,
      languages: [
        ...others,
        { name: name.trim(), level: level.trim() as PreferenceLanguage["level"], fromResume: false },
      ],
    };
  }
  return prefs;
}

// The fields a hard filter reads. A change to any of them is what a preview is for; a change
// elsewhere (notes, batch size) moves no stored job, so it asks for nothing.
function filterKey(prefs: Preferences): string {
  const terms = (values: Term[]) => values.map((value) => value.term);
  return JSON.stringify([
    terms(prefs.titleTerms),
    terms(prefs.excludedRoleTerms),
    terms(prefs.excludedStack),
    terms(prefs.coreSkills),
    prefs.seniority,
    terms(prefs.allowedCities),
    prefs.allowRemote,
    prefs.requirePermanent,
    prefs.languages.map((language) => [language.name, language.level]),
  ]);
}

const PREVIEW_DELAY_MS = 400;

function previewSummary(preview: PreferencesPreview): string {
  if (!preview.changed) return "Saving would not move any of your stored jobs.";
  const moved = Object.entries(preview.transitions)
    .map(([transition, count]) => `${count} ${transition}`)
    .join(", ");
  return `Saving would re-filter ${preview.examined} stored jobs: ${moved || "reasons updated"}. New jobs are filtered the same way at the next search.`;
}

function summarize(result: SaveResult): string {
  const moved = Object.entries(result.refiltered.transitions)
    .map(([transition, count]) => `${count} ${transition}`)
    .join(", ");
  const filtered = result.refiltered.changed
    ? `Re-filtered ${result.refiltered.examined} stored jobs: ${moved || "reasons updated"}.`
    : `Re-filtered ${result.refiltered.examined} stored jobs; nothing moved.`;
  return `Saved as version ${result.preferences.version}. ${filtered} ${result.reopenedForScoring} would be scored on the next run.`;
}

export function PreferencesForm() {
  const [prefs, setPrefs] = useState<Preferences | null>(null);
  const [boardsAdded, setBoardsAdded] = useState(0);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [baseline, setBaseline] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreferencesPreview | null>(null);

  useEffect(() => {
    void getPreferences()
      .then((loaded) => {
        setPrefs(loaded);
        setBaseline(filterKey(loaded));
      })
      .catch((failure: unknown) => {
        setError(failure instanceof Error ? failure.message : "Something went wrong.");
      });
  }, []);

  const key = prefs ? filterKey(prefs) : null;
  useEffect(() => {
    if (!prefs || key === baseline) return;
    let current = true;
    const timer = setTimeout(() => {
      previewPreferences(prefs)
        .then((result) => current && setPreview(result))
        // A draft that cannot be previewed (a field is invalid) just shows no preview; the
        // save is what reports the field errors.
        .catch(() => current && setPreview(null));
    }, PREVIEW_DELAY_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
    // `key` stands for the filter-relevant part of `prefs`; depending on `prefs` itself
    // would refetch on every keystroke in the notes box.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, baseline]);

  // A preview describes a draft that differs from what is saved; once the draft is back to
  // the saved state there is nothing to say, whatever the last response was.
  const shown = key === baseline ? null : preview;

  if (error && !prefs) {
    return (
      <p className="form-error" role="alert">
        {error}
      </p>
    );
  }
  if (!prefs) return <p className="pref-hint">Loading your preferences…</p>;

  const update = (patch: Partial<Preferences>) => setPrefs({ ...prefs, ...patch });
  // A collapsed section would otherwise hide the message that explains a failed save.
  const roleRulesHaveError = Boolean(
    errors.title_terms ||
      errors.core_skills ||
      errors.excluded_stack ||
      errors.excluded_role_terms ||
      errors.role_families,
  );
  const runSizeHasError = Boolean(errors.batch_size || errors.max_per_company);

  async function save() {
    if (!prefs) return;
    setSaving(true);
    setErrors({});
    setError(null);
    setMessage(null);
    try {
      const result = await savePreferences(prefs);
      setPrefs(result.preferences);
      setBaseline(filterKey(result.preferences));
      setMessage(summarize(result));
    } catch (failure) {
      if (failure instanceof ValidationError) setErrors(failure.errors);
      else setError(failure instanceof Error ? failure.message : "Something went wrong.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <p aria-live="polite" className="live-status">
        {saving ? "Saving your preferences." : (message ?? error ?? "")}
      </p>

      <CompanySummary refreshKey={boardsAdded} />
      <AddCompanyBoard onAdded={() => setBoardsAdded((count) => count + 1)} />

      <section className="pref-section" aria-labelledby="roles-heading">
        <div className="pref-heading-row">
          <h2 id="roles-heading">What you are looking for</h2>
          <InfoTip label="About what you are looking for">
            Read by the agent that ranks and curates postings, not matched against them as rules.
          </InfoTip>
        </div>
        <IntentLog />
        <SampleRoles />
        <Disclosure
          forceOpen={roleRulesHaveError}
          info="The structured form of what you wrote above, plus four title and stack gates. Leaving them alone is a valid choice: an empty gate is off, not strict."
          level="group"
          title="More role rules"
        >
          <ChipInput
            error={errors.role_families}
            hint="Up to five. Read by the model alongside what you wrote above."
            label="Role families"
            max={MAX_ROLE_FAMILIES}
            onChange={(roleFamilies) => update({ roleFamilies })}
            values={prefs.roleFamilies}
          />
          <ChipInput
            error={errors.title_terms}
            hint="Matched against the job title. A title naming none of these is parked for review, never dropped. Empty turns the gate off."
            label="Role title terms"
            onChange={(titleTerms) => update({ titleTerms })}
            values={prefs.titleTerms}
          />
          <ChipInput
            error={errors.core_skills}
            hint="Pulled from your resume, edit freely. A posting naming none of these is rejected before any model call."
            label="Skills & stack"
            onChange={(coreSkills) => update({ coreSkills })}
            values={prefs.coreSkills}
          />
          <ChipInput
            error={errors.excluded_stack}
            hint="Counted against the skills above. A posting dominated by these is parked for review."
            label="Excluded stack"
            onChange={(excludedStack) => update({ excludedStack })}
            values={prefs.excludedStack}
          />
          <ChipInput
            error={errors.excluded_role_terms}
            hint="Matched against the job title only. A hit rejects the posting."
            label="Excluded role terms"
            onChange={(excludedRoleTerms) => update({ excludedRoleTerms })}
            values={prefs.excludedRoleTerms}
          />
          {shown
            ? prefs.excludedRoleTerms
                .filter((value) => shown.excludedRoleTerms[value.term]?.matches)
                .map((value) => {
                  const impact = shown.excludedRoleTerms[value.term];
                  return (
                    <p className="pref-hint" key={value.term} role="status">
                      <strong>{value.term}</strong> is in {impact.matches} stored job title
                      {impact.matches === 1 ? "" : "s"}
                      {impact.newlyHidden
                        ? `; ${impact.newlyHidden} would be newly hidden.`
                        : "; none would be newly hidden."}
                    </p>
                  );
                })
            : null}
        </Disclosure>
      </section>

      <section className="pref-section" aria-labelledby="seniority-heading">
        <Disclosure
          forceOpen={Boolean(errors.seniority)}
          headingId="seniority-heading"
          info="A band you do not select is rejected on the title. Selecting one stops that rule firing."
          title="Seniority"
        >
          <div className="pill-row" role="group" aria-labelledby="seniority-heading">
            {prefs.seniorityBands.map((band) => {
              const selected = prefs.seniority.includes(band);
              return (
                <button
                  aria-pressed={selected}
                  className={`pill${selected ? " pill-selected" : ""}`}
                  key={band}
                  onClick={() =>
                    update({
                      seniority: selected
                        ? prefs.seniority.filter((other) => other !== band)
                        : [...prefs.seniority, band],
                    })
                  }
                  type="button"
                >
                  {band}
                </button>
              );
            })}
          </div>
          {errors.seniority ? (
            <p className="form-error" role="alert">
              {errors.seniority}
            </p>
          ) : null}
        </Disclosure>
      </section>

      <section className="pref-section" aria-labelledby="location-heading">
        <div className="pref-heading-row">
          <h2 id="location-heading">Locations & remote</h2>
          <InfoTip label="About locations and remote">
            Allowed locations is the hard gate. Preferred and flagged only colour a posting
            that already passed it; remote is matched on the posting&rsquo;s own flag.
          </InfoTip>
        </div>
        <ChipInput
          error={errors.allowed_cities}
          hint="A posting located anywhere else is rejected."
          label="Allowed locations"
          onChange={(allowedCities) => update({ allowedCities })}
          values={prefs.allowedCities}
        />
        <Disclosure
          info="Nothing here can reject a posting; these rank one that already passed, and flag it for you to confirm."
          level="group"
          title="More location rules"
        >
          <ChipInput
            label="Preferred"
            onChange={(preferredCities) => update({ preferredCities })}
            values={prefs.preferredCities}
          />
          <ChipInput
            hint="Kept, but flagged for you to confirm the arrangement."
            label="Flagged"
            onChange={(flaggedCities) => update({ flaggedCities })}
            values={prefs.flaggedCities}
          />
          <label className="switch">
            <input
              checked={prefs.allowRemote}
              onChange={(event) => update({ allowRemote: event.target.checked })}
              type="checkbox"
            />
            Include remote roles
          </label>
        </Disclosure>
      </section>

      <section className="pref-section" aria-labelledby="languages-heading">
        <Disclosure
          forceOpen={Boolean(errors.languages)}
          headingId="languages-heading"
          info="The level you have. A posting demanding German above it is rejected; one demanding German when you have not listed it is parked for review. Other languages inform model scoring."
          title="Languages"
        >
          <ul className="language-list">
            {prefs.languages.map((language, index) => (
              <li className="language-row" key={language.name}>
                <span>
                  {language.name}
                  {language.fromResume ? <span className="chip-badge">RESUME</span> : null}
                </span>
                <label className="visually-hidden" htmlFor={`level-${language.name}`}>
                  {language.name} level
                </label>
                <select
                  id={`level-${language.name}`}
                  onChange={(event) =>
                    update({
                      languages: prefs.languages.map((other, position) =>
                        position === index
                          ? { ...other, level: event.target.value as PreferenceLanguage["level"] }
                          : other,
                      ),
                    })
                  }
                  value={language.level}
                >
                  {prefs.cefrLevels.map((level) => (
                    <option key={level} value={level}>
                      {level}
                    </option>
                  ))}
                </select>
                <button
                  aria-label={`Remove ${language.name}`}
                  className="ghost-action"
                  onClick={() =>
                    update({
                      languages: prefs.languages.filter((_, position) => position !== index),
                    })
                  }
                  type="button"
                >
                  REMOVE
                </button>
              </li>
            ))}
          </ul>
          <AddLanguage
            levels={prefs.cefrLevels}
            onAdd={(language) => update({ languages: [...prefs.languages, language] })}
          />
          {errors.languages ? (
            <p className="form-error" role="alert">
              {errors.languages}
            </p>
          ) : null}
        </Disclosure>
      </section>

      <section className="pref-section" aria-labelledby="run-heading">
        <Disclosure
          forceOpen={runSizeHasError}
          headingId="run-heading"
          info="Neither number reaches a prompt, so changing them re-scores nothing. Permanent-only is a hard gate on the contract type."
          title="Employment & run size"
        >
          <label className="switch">
            <input
              checked={prefs.requirePermanent}
              onChange={(event) => update({ requirePermanent: event.target.checked })}
              type="checkbox"
            />
            Permanent employment only
          </label>
          <label className="number-field">
            Postings scored per run
            <input
              min={1}
              onChange={(event) => update({ batchSize: Number(event.target.value) })}
              type="number"
              value={prefs.batchSize}
            />
          </label>
          {errors.batch_size ? (
            <p className="form-error" role="alert">
              {errors.batch_size}
            </p>
          ) : null}
          <label className="number-field">
            Most postings kept per company
            <input
              min={1}
              onChange={(event) => update({ maxPerCompany: Number(event.target.value) })}
              type="number"
              value={prefs.maxPerCompany}
            />
          </label>
          {errors.max_per_company ? (
            <p className="form-error" role="alert">
              {errors.max_per_company}
            </p>
          ) : null}
        </Disclosure>
      </section>

      <NotesExtraction
        notes={prefs.notes}
        onAccept={(change) => setPrefs((current) => (current ? applyChange(current, change) : current))}
        onNotesChange={(notes) => update({ notes })}
        preferences={prefs}
      />

      {error ? (
        <p className="form-error" role="alert">
          {error}
        </p>
      ) : null}
      {message ? <p className="save-report">{message}</p> : null}
      {shown ? (
        <p className="pref-hint" role="status">
          {previewSummary(shown)}
        </p>
      ) : null}
      <div className="form-actions">
        {prefs.version > 0 ? (
          <Link className="primary-action" href="/recommendations">
            Continue to Recommendations
          </Link>
        ) : null}
        <button className="secondary-action" disabled={saving} onClick={() => void save()} type="button">
          {saving ? "Saving…" : "Save preferences"}
        </button>
      </div>
    </>
  );
}

function AddLanguage({
  levels,
  onAdd,
}: {
  levels: PreferenceLanguage["level"][];
  onAdd: (language: PreferenceLanguage) => void;
}) {
  const [name, setName] = useState("");
  const [level, setLevel] = useState<PreferenceLanguage["level"]>(levels[0] ?? "B1");

  return (
    <div className="chip-add">
      <label className="visually-hidden" htmlFor="new-language">
        Add a language
      </label>
      <input
        id="new-language"
        onChange={(event) => setName(event.target.value)}
        placeholder="Add a language"
        value={name}
      />
      <label className="visually-hidden" htmlFor="new-language-level">
        Level for the new language
      </label>
      <select
        id="new-language-level"
        onChange={(event) => setLevel(event.target.value as PreferenceLanguage["level"])}
        value={level}
      >
        {levels.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
      <button
        className="ghost-action"
        onClick={() => {
          if (!name.trim()) return;
          onAdd({ name: name.trim(), level, fromResume: false });
          setName("");
        }}
        type="button"
      >
        ADD
      </button>
    </div>
  );
}
