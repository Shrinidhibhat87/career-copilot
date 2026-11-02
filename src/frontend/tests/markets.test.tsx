import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MarketsSection } from "@/components/markets-section";
import type { MarketsView } from "@/lib/api";

const respond = (body: unknown, status = 200) =>
  ({ ok: status < 400, status, json: async () => body }) as Response;

const VIEW: MarketsView = {
  packs: [
    { code: "DE", name: "Germany", language: "german", ready: true },
    { code: "FR", name: "France", language: "french", ready: false },
  ],
  enabled: ["DE"],
  confirmed: false,
  proposal: ["DE"],
};

function stub(put: () => Response = () => respond({ ...VIEW, confirmed: true })) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${init?.body ?? ""}`);
      return init?.method === "PUT" ? put() : respond(VIEW);
    }),
  );
  return calls;
}

beforeEach(() => vi.unstubAllGlobals());
afterEach(() => vi.unstubAllGlobals());

describe("MarketsSection", () => {
  it("shows the default, the suggestion, and coming-soon markets as disabled", async () => {
    stub();
    render(<MarketsSection />);
    expect(await screen.findByRole("checkbox", { name: "Germany" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /France/ })).toBeDisabled();
    expect(screen.getByText(/Not confirmed yet, so Germany is used by default/)).toBeInTheDocument();
    expect(screen.getByText(/Your saved cities suggest Germany/)).toBeInTheDocument();
  });

  it("confirming saves the chosen markets and says so", async () => {
    const calls = stub();
    render(<MarketsSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Confirm markets" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Saved");
    expect(calls).toContain('PUT {"codes":["DE"]}');
    expect(screen.getByRole("button", { name: "Save markets" })).toBeInTheDocument();
    expect(screen.queryByText(/Not confirmed yet/)).not.toBeInTheDocument();
  });

  it("cannot save with nothing chosen", async () => {
    stub();
    render(<MarketsSection />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Germany" }));
    expect(screen.getByRole("button", { name: "Confirm markets" })).toBeDisabled();
  });

  it("shows the server's refusal", async () => {
    stub(() => respond({ detail: { errors: { codes: "not available yet: FR" } } }, 400));
    render(<MarketsSection />);
    fireEvent.click(await screen.findByRole("button", { name: "Confirm markets" }));
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("not available yet: FR"),
    );
  });
});
