import Link from "next/link";

import { AccountMenu } from "@/components/account-menu";
import { AutoRefresh } from "@/components/auto-refresh";

// "sign-in" and "resume" have no tab: Profile lives in the account menu, and the sign-in
// page is where nobody is signed in yet.
type Step = "sign-in" | "resume" | "preferences" | "review" | "settings";

const tabs = [
  { id: "preferences", label: "PREFERENCES", href: "/preferences" },
  { id: "review", label: "RECOMMENDATIONS", href: "/recommendations" },
  { id: "settings", label: "SETTINGS", href: "/settings" },
] as const;

export function AppHeader({ currentStep }: { currentStep: Step }) {
  return (
    <header className="app-header">
      <Link className="wordmark" href="/" aria-label="Career Copilot home">
        Career Copilot
      </Link>
      <nav aria-label="Main">
        <ul className="nav-tabs">
          {tabs.map((tab) => (
            <li key={tab.id}>
              <Link
                className="nav-tab"
                href={tab.href}
                aria-current={tab.id === currentStep ? "page" : undefined}
              >
                {tab.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <AccountMenu />
      <AutoRefresh />
    </header>
  );
}
