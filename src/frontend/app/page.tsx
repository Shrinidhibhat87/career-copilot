import { AppHeader } from "@/components/app-header";
import { SignInActions } from "@/components/sign-in-actions";

function BrandPanel() {
  return (
    <section className="brand-panel" aria-labelledby="brand-heading">
      <p className="eyebrow">V1 · LOCAL FIRST · CLOUD DEPLOYABLE</p>
      <div className="brand-message">
        <h1 id="brand-heading">A quieter way to read the job market.</h1>
        <p>
          Career Copilot finds postings, filters the obvious misses, and shows you its reasoning.
          <br />
          You stay the one who decides.
        </p>
      </div>
      <div className="source-stat">
        <span className="source-stat-value">8</span>
        <span className="source-stat-label">sources supported</span>
      </div>
    </section>
  );
}

export default function SignInPage() {
  return (
    <div className="app-shell">
      <AppHeader currentStep="sign-in" />
      <main className="sign-in-layout">
        <BrandPanel />
        <SignInActions />
      </main>
    </div>
  );
}
