import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NotificationsSection } from "@/components/notifications-section";
import type { NotificationsView } from "@/lib/api";

const respond = (body: unknown, status = 200) =>
  ({ ok: status < 400, status, json: async () => body }) as Response;

const BLANK: NotificationsView = {
  enabled: false,
  keychain: true,
  telegram: { tokenSet: false, paired: false, on: false, masked: null, lastFailure: null },
  email: { setUp: false, on: false, masked: null, lastFailure: null },
};
const PAIRED: NotificationsView = {
  ...BLANK,
  telegram: { tokenSet: true, paired: true, on: false, masked: "chat …4821", lastFailure: null },
};

/** Answers by method and path; records each call so a test can read what was sent. */
function stub(routes: Record<string, () => Response>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url).replace(/^.*\/api\/notifications/, "");
      const key = `${init?.method ?? "GET"} ${path}`;
      calls.push(init?.body ? `${key} ${String(init.body)}` : key);
      const route = routes[key];
      if (!route) throw new Error(`unexpected ${key}`);
      return route();
    }),
  );
  return calls;
}

/** The section rests collapsed, like the others on Settings. */
const open = () => fireEvent.click(screen.getByRole("button", { name: /^Notifications/ }));

beforeEach(() => vi.unstubAllGlobals());
afterEach(() => vi.unstubAllGlobals());

describe("Notifications", () => {
  it("starts collapsed, with nothing set up and everything off", async () => {
    stub({ "GET ": () => respond(BLANK) });
    render(<NotificationsSection />);
    expect(screen.getByRole("button", { name: /^Notifications/ })).toHaveAttribute("aria-expanded", "false");
    open();
    expect(await screen.findAllByText("Not set up")).toHaveLength(2);
    expect(screen.getByRole("switch", { name: "Send notifications" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("switch", { name: "Telegram" })).toBeDisabled();
    expect(screen.getByRole("switch", { name: "Email" })).toBeDisabled();
  });

  it("says plainly that messages leave the machine", async () => {
    stub({ "GET ": () => respond(BLANK) });
    render(<NotificationsSection />);
    open();
    expect(await screen.findByText(/Messages leave this machine/)).toBeInTheDocument();
  });

  it("shows only the masked chat once paired, and lets it be switched on and saved", async () => {
    const calls = stub({
      "GET ": () => respond(PAIRED),
      "PUT ": () => respond({ ...PAIRED, enabled: true, telegram: { ...PAIRED.telegram, on: true } }),
    });
    render(<NotificationsSection />);
    open();
    expect(await screen.findByText("Paired, chat …4821")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "Send notifications" }));
    fireEvent.click(screen.getByRole("switch", { name: "Telegram" }));
    fireEvent.click(screen.getByRole("button", { name: "Save notifications" }));
    expect(await screen.findByText(/Saved\./)).toBeInTheDocument();
    expect(calls).toContain('PUT  {"enabled":true,"telegram":true,"email":false}');
  });

  it("pairs Telegram: token, then a code, then done once the chat answers", async () => {
    let paired = false;
    stub({
      "GET ": () => respond(paired ? PAIRED : BLANK),
      "PUT /telegram": () => respond({ code: "a1b2c3", bot: "my_bot" }),
      "GET /telegram/pairing": () => {
        paired = true;
        return respond(PAIRED.telegram);
      },
    });
    render(<NotificationsSection />);
    open();
    await screen.findAllByText("Not set up");
    fireEvent.click(screen.getAllByRole("button", { name: "Set up" })[0]);
    const dialog = screen.getByRole("dialog", { name: "Set up Telegram" });
    fireEvent.change(within(dialog).getByLabelText("Bot token"), { target: { value: "123456789:abc" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("a1b2c3")).toBeInTheDocument();
    expect(screen.getByText("@my_bot")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "I sent it" }));
    expect(await screen.findByText("Paired, chat …4821")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows a refused token and keeps the form", async () => {
    stub({
      "GET ": () => respond(BLANK),
      "PUT /telegram": () =>
        respond({ detail: { errors: { token: "Telegram refused the request. Check the bot token." } } }, 422),
    });
    render(<NotificationsSection />);
    open();
    await screen.findAllByText("Not set up");
    fireEvent.click(screen.getAllByRole("button", { name: "Set up" })[0]);
    fireEvent.change(screen.getByLabelText("Bot token"), { target: { value: "123456789:abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText(/Check the bot token/)).toBeInTheDocument();
    expect(screen.getByLabelText("Bot token")).toBeInTheDocument();
  });

  it("saves email details and shows only the masked address", async () => {
    let set = false;
    const calls = stub({
      "GET ": () =>
        respond(
          set ? { ...BLANK, email: { setUp: true, on: false, masked: "j***@gmail.com", lastFailure: null } } : BLANK,
        ),
      "PUT /email": () => {
        set = true;
        return respond(BLANK);
      },
    });
    render(<NotificationsSection />);
    open();
    await screen.findAllByText("Not set up");
    fireEvent.click(screen.getAllByRole("button", { name: "Set up" })[1]);
    const fill = (label: string, value: string) =>
      fireEvent.change(screen.getByLabelText(label), { target: { value } });
    fill("Your email address", "jane@gmail.com");
    fill("App password", "secret-pass");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByText("Sends to j***@gmail.com")).toBeInTheDocument();
    // Gmail's server is worked out from the address; the person typed two things.
    expect(calls.find((c) => c.startsWith("PUT /email"))).toContain('"host":"smtp.gmail.com","port":587');
    expect(screen.queryByText(/secret-pass/)).not.toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Email" })).toBeEnabled();
  });

  it("asks for the server only when it cannot tell the provider", async () => {
    stub({ "GET ": () => respond(BLANK) });
    render(<NotificationsSection />);
    open();
    await screen.findAllByText("Not set up");
    fireEvent.click(screen.getAllByRole("button", { name: "Set up" })[1]);
    fireEvent.change(screen.getByLabelText("Your email address"), { target: { value: "me@mycompany.org" } });
    fireEvent.change(screen.getByLabelText("App password"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/do not know this mail provider/);
  });

  it("lets the Saved note fade after a few seconds", async () => {
    stub({
      "GET ": () => respond(PAIRED),
      "PUT ": () => respond(PAIRED),
    });
    render(<NotificationsSection />);
    open();
    await screen.findByText("Paired, chat …4821");
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      fireEvent.click(screen.getByRole("button", { name: "Save notifications" }));
      expect(await screen.findByText(/Saved\./)).toBeInTheDocument();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4100);
      });
      expect(screen.queryByText(/Saved\./)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("reports a test result and shows a failed send in words", async () => {
    stub({
      "GET ": () =>
        respond({
          ...PAIRED,
          telegram: {
            ...PAIRED.telegram,
            lastFailure: { message: "Telegram could not be reached.", at: "2026-11-04T09:00:00Z" },
          },
        }),
      "POST /telegram/test": () => respond({ ok: true, error: null }),
    });
    render(<NotificationsSection />);
    open();
    expect(await screen.findByText(/Telegram could not be reached/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Send test" }));
    expect(await screen.findByText("Test message sent.")).toBeInTheDocument();
  });

  it("removes Telegram", async () => {
    const calls = stub({
      "GET ": () => respond(PAIRED),
      "DELETE /telegram": () => respond(BLANK),
    });
    render(<NotificationsSection />);
    open();
    await screen.findByText("Paired, chat …4821");
    fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(screen.getAllByText("Not set up")).toHaveLength(2));
    expect(calls).toContain("DELETE /telegram");
  });

  it("opens on a refused save and shows why", async () => {
    stub({
      "GET ": () => respond(PAIRED),
      "PUT ": () =>
        respond({ detail: { errors: { telegram: "set up and pair Telegram before switching it on" } } }, 422),
    });
    render(<NotificationsSection />);
    open();
    await screen.findByText("Paired, chat …4821");
    fireEvent.click(screen.getByRole("button", { name: "Save notifications" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/before switching it on/);
  });
});
