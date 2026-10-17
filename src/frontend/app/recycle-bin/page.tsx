import { AppHeader } from "@/components/app-header";
import { RecycleBin } from "@/components/recycle-bin";

export default function RecycleBinPage() {
  return (
    <div className="app-shell">
      <AppHeader currentStep="review" />
      <main className="review-page">
        <RecycleBin />
      </main>
    </div>
  );
}
