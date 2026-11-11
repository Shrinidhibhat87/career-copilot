import { AppHeader } from "@/components/app-header";
import { ApplicationBoard } from "@/components/application-board";

export default function ApplicationsPage() {
  return (
    <div className="app-shell">
      <AppHeader currentStep="applications" />
      <main className="review-page">
        <ApplicationBoard />
      </main>
    </div>
  );
}
