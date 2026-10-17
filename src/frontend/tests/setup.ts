import { vi } from "vitest";
import "@testing-library/jest-dom/vitest";

// The header asks for an auto refresh on mount. Page tests script their own fetch calls,
// so the component is stubbed everywhere; its own test loads the real one.
vi.mock("@/components/auto-refresh", () => ({ AutoRefresh: () => null }));

// Same for the account menu: it asks the server who is signed in on mount.
vi.mock("@/components/account-menu", () => ({ AccountMenu: () => null }));
