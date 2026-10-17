import { AppHeader } from "@/components/app-header";
import { PreferencesForm } from "@/components/preferences-form";

export default function PreferencesPage() {
  return (
    <div className="app-shell">
      <AppHeader currentStep="preferences" />
      <main className="step-page">
        <section aria-labelledby="preferences-heading">
          <p className="eyebrow dark-eyebrow">STEP 3 OF 4 · PREFERENCES</p>
          <h1 id="preferences-heading">What should it look for?</h1>
          <p className="step-intro">
            These decide what gets collected, what is thrown out before any model sees it, and
            what the model is told you want. Saving re-checks the postings already stored.
          </p>
          <PreferencesForm />
        </section>
      </main>
    </div>
  );
}
