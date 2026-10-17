import { AppHeader } from "@/components/app-header";
import { ResumeUpload } from "@/components/resume-upload";

export default function ResumePage() {
  return (
    <div className="app-shell">
      <AppHeader currentStep="resume" />
      <main className="step-page">
        <section aria-labelledby="resume-heading">
          <p className="eyebrow dark-eyebrow">STEP 2 OF 4 · PROFILE</p>
          <h1 id="resume-heading">Start with your resume.</h1>
          <p className="step-intro">
            Everything on the next screen is pre-filled from this file, and every match claim later
            has to quote it. The file stays on this machine; only redacted excerpts are ever sent to
            a model.
          </p>
          <ResumeUpload />
        </section>
      </main>
    </div>
  );
}
