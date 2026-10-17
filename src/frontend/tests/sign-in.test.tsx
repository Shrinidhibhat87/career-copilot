import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import SignInPage from "@/app/page";

const push = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

type Status = { accounts: string[]; user: string | null; adoptsExisting: boolean };

/** Scripts the API: the status read, then whatever each later call answers. */
function server(status: Status, later: Record<string, { status: number; body?: object }> = {}) {
  const fetchMock = vi.fn<typeof fetch>(async (input) => {
    const url = String(input);
    if (url.endsWith("/api/auth/status")) return new Response(JSON.stringify(status));
    const answer = later[url.replace(/^.*\/api\//, "")] ?? { status: 200 };
    return new Response(JSON.stringify(answer.body ?? {}), { status: answer.status });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const EMPTY: Status = { accounts: [], user: null, adoptsExisting: false };
const ONE: Status = { accounts: ["anna"], user: null, adoptsExisting: false };

describe("sign-in page", () => {
  beforeEach(() => {
    push.mockClear();
    vi.unstubAllGlobals();
  });

  it("renders the product context and progress", () => {
    server(ONE);
    render(<SignInPage />);
    expect(screen.getByText("A quieter way to read the job market.")).toBeInTheDocument();
    for (const label of ["PREFERENCES", "RECOMMENDATIONS", "SETTINGS"]) {
      expect(screen.getByRole("link", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("link", { name: "SETTINGS" })).toHaveAttribute("href", "/settings");
  });

  it("opens in create mode when no account exists", async () => {
    server(EMPTY);
    render(<SignInPage />);
    expect(await screen.findByText("Create your account")).toBeInTheDocument();
    expect(screen.getByLabelText("Confirm passcode")).toBeInTheDocument();
  });

  it("says the old data moves into the first account", async () => {
    server({ ...EMPTY, adoptsExisting: true });
    render(<SignInPage />);
    expect(await screen.findByText(/existing CV, preferences and decisions/)).toBeInTheDocument();
  });

  it("opens in sign-in mode with the accounts as chips, and the link flips the form", async () => {
    server(ONE);
    render(<SignInPage />);
    expect(await screen.findByText("Welcome back")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "anna" }));
    expect(screen.getByLabelText("Username")).toHaveValue("anna");
    expect(screen.queryByLabelText("Confirm passcode")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByText("Create your account")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Welcome back")).toBeInTheDocument();
  });

  it("signs in and goes to the recommendations when a CV exists", async () => {
    const fetchMock = server(ONE, { "resume": { status: 200 } });
    render(<SignInPage />);
    await screen.findByText("Welcome back");
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "anna" } });
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret1" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/recommendations"));
    const login = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/auth/login"));
    expect(JSON.parse(String(login?.[1]?.body))).toEqual({ name: "anna", passcode: "secret1" });
  });

  it("goes to the resume page after creating an account with no CV", async () => {
    server(EMPTY, { resume: { status: 404 } });
    render(<SignInPage />);
    await screen.findByText("Create your account");
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "anna" } });
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret1" } });
    fireEvent.change(screen.getByLabelText("Confirm passcode"), { target: { value: "secret1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/resume"));
  });

  it("refuses a mismatched or short passcode without calling the server", async () => {
    const fetchMock = server(EMPTY);
    render(<SignInPage />);
    await screen.findByText("Create your account");
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "anna" } });
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "abc" } });
    fireEvent.change(screen.getByLabelText("Confirm passcode"), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("at least 6 characters");
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("do not match");
    expect(fetchMock).toHaveBeenCalledTimes(1); // the status read only
  });

  it("shows the server's refusal and stays", async () => {
    server(ONE, { "auth/login": { status: 401, body: { detail: "That username and passcode do not match." } } });
    render(<SignInPage />);
    await screen.findByText("Welcome back");
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "anna" } });
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "wrong-one" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("do not match");
    expect(push).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled();
  });

  it("skips the form when someone is already signed in", async () => {
    server({ accounts: ["anna"], user: "anna", adoptsExisting: false }, { resume: { status: 200 } });
    render(<SignInPage />);
    await waitFor(() => expect(push).toHaveBeenCalledWith("/recommendations"));
  });

  it("says so when the server cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    render(<SignInPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");
  });
});

describe("importing an export", () => {
  beforeEach(() => {
    push.mockClear();
    vi.unstubAllGlobals();
  });

  const pick = (file: File) =>
    fireEvent.change(screen.getByLabelText("Export file"), { target: { files: [file] } });

  it("is offered in create mode only", async () => {
    server(ONE);
    render(<SignInPage />);
    await screen.findByText("Welcome back");
    expect(screen.queryByLabelText("Export file")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByLabelText("Export file")).toBeInTheDocument();
  });

  it("sends the chosen file to the import route", async () => {
    const fetchMock = server(EMPTY, { resume: { status: 200 } });
    render(<SignInPage />);
    await screen.findByText("Create your account");
    pick(new File(["x"], "career-copilot-anna.db"));
    expect(screen.getByText("career-copilot-anna.db")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "anna" } });
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret1" } });
    fireEvent.change(screen.getByLabelText("Confirm passcode"), { target: { value: "secret1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/recommendations"));
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/auth/import"))).toBe(true);
  });

  it("shows why a file was refused", async () => {
    server(EMPTY, {
      "auth/import": { status: 422, body: { detail: "That file is not a Career Copilot export." } },
    });
    render(<SignInPage />);
    await screen.findByText("Create your account");
    pick(new File(["x"], "junk.db"));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "anna" } });
    fireEvent.change(screen.getByLabelText("Passcode"), { target: { value: "secret1" } });
    fireEvent.change(screen.getByLabelText("Confirm passcode"), { target: { value: "secret1" } });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("not a Career Copilot export");
    expect(push).not.toHaveBeenCalled();
  });
});
