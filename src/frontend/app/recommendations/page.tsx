import { AppHeader } from "@/components/app-header";
import { ReviewScreen } from "@/components/review-screen";

export default function RecommendationsPage() {
  return (
    <div className="app-shell">
      <AppHeader currentStep="review" />
      <main className="review-page">
        <ReviewScreen />
      </main>
    </div>
  );
}
