import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as Module from "@/components/account-menu";

// `tests/setup.ts` stubs the menu for page tests; this file needs the real one.
const { AccountMenu } = await vi.importActual<typeof Module>("@/components/account-menu");

function server(user: string | null, logout: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL) =>
      String(input).endsWith("/status")
        ? new Response(JSON.stringify({ accounts: [], user, adoptsExisting: false }))
        : new Response(JSON.stringify({ detail: "A run is active." }), { status: logout }),
    ),
  );
}

describe("account menu", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("renders nothing when nobody is signed in", async () => {
    server(null, 204);
    const { container } = render(<AccountMenu />);
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the person and explains a refused sign-out", async () => {
    server("anna", 409);
    render(<AccountMenu />);
    fireEvent.click(await screen.findByRole("button", { name: "Account: anna" }));
    expect(screen.getByRole("menuitem", { name: "Profile" })).toHaveAttribute("href", "/resume");
    expect(screen.getByRole("menuitem", { name: "Update CV" })).toHaveAttribute("href", "/resume");
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("A run is active.");
    expect(screen.getByRole("menuitem", { name: "Sign out" })).toBeEnabled();
  });

  it("closes on Escape", async () => {
    server("anna", 204);
    render(<AccountMenu />);
    fireEvent.click(await screen.findByRole("button", { name: "Account: anna" }));
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });
});
