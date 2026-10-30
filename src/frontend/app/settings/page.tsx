import { AccountExport } from "@/components/account-export";
import { AppHeader } from "@/components/app-header";
import { ModelSettings } from "@/components/model-settings";
import { RunsScreen } from "@/components/runs-screen";
import { SourceHealth } from "@/components/source-health";

export default function SettingsPage() {
  return (
    <div className="app-shell">
      <AppHeader currentStep="settings" />
      <main className="settings-page">
        <h1>Settings</h1>
        <ModelSettings />
        <RunsScreen />
        <SourceHealth />
        <AccountExport />
      </main>
    </div>
  );
}
