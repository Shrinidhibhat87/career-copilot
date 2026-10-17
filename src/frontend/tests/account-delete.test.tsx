import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AccountExport } from "@/components/account-export";

function server(status: number, detail = "") {
  const fetchMock = vi.fn(async () => new Response(status === 204 ? null : JSON.stringify({ detail }), { status }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function openDelete() {
  fireEvent.click(screen.getByRole("button", { name: /Delete this account/ }));
  fireEvent.click(screen.getByRole("button", { name: "Delete my account" }));
}

describe("delete account", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("asks for the passcode before anything is sent", () => {
    const fetchMock = server(204);
    render(<AccountExport />);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    openDelete();
    expect(screen.getByRole("alertdialog")).toHaveTextContent("Delete this account?");
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("is two collapsed sections, closed until opened", () => {
    server(204);
    render(<AccountExport />);
    expect(screen.getByRole("button", { name: /Download your data/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.getByRole("button", { name: /Delete this account/ })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  it("goes back to the first page once the account is deleted", async () => {
    server(204);
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({ assign } as unknown as Location);
    render(<AccountExport />);
    openDelete();
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret1" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete everything" }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
    vi.restoreAllMocks();
  });

  it("shows the refusal and keeps the form when the passcode is wrong", async () => {
    const fetchMock = server(401, "That passcode does not match.");
    render(<AccountExport />);
    openDelete();
    fireEvent.change(screen.getByLabelText("Passcode"), {
      target: { value: "wrong-one" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Delete everything" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("does not match");
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/api\/auth\/account$/);
    expect(init.method).toBe("DELETE");
    expect(JSON.parse(String(init.body))).toEqual({ passcode: "wrong-one" });
    expect(screen.getByRole("button", { name: "Delete everything" })).toBeEnabled();
  });
});
