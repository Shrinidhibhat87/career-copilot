import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { IntentLog } from "@/components/intent-log";
import { SampleRoles } from "@/components/sample-roles";

function respond(status: number, body?: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

type Handler = (init: RequestInit | undefined) => ReturnType<typeof respond>;

/** Routes each request by method and path, so a test names only the calls it cares about
 * and the order the component makes them in does not matter. */
function serve(routes: Record<string, Handler>) {
  const fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
    const path = new URL(input, "http://local").pathname;
    const handler = routes[`${init?.method ?? "GET"} ${path}`];
    if (!handler) throw new Error(`unexpected request ${init?.method ?? "GET"} ${path}`);
    return handler(init);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const bodyOf = (init: RequestInit | undefined) =>
  JSON.parse(init?.body as string) as Record<string, unknown>;

const firstEntry = {
  id: 1,
  text: "Forward-deployed roles with customers",
  kind: "entry",
  createdAt: "2026-10-01T09:00:00Z",
};

const secondEntry = {
  id: 2,
  text: "Not adtech, ever",
  kind: "entry",
  createdAt: "2026-10-03T09:00:00Z",
};

// Past entries are collapsed by default; tests that read the list open it first.
async function openPast() {
  fireEvent.click(await screen.findByRole("button", { name: /Past entries/ }));
}

describe("intent log", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("keeps past entries collapsed until the user opens them", async () => {
    serve({
      "GET /api/intent": () => respond(200, { entries: [firstEntry], activeWords: 4, condenseAt: 1000 }),
    });
    render(<IntentLog />);
    const toggle = await screen.findByRole("button", { name: "Past entries (1)" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText(firstEntry.text)).not.toBeVisible();
    fireEvent.click(toggle);
    expect(screen.getByText(firstEntry.text)).toBeVisible();
  });

  it("adds an entry, shows it, and updates the word count", async () => {
    const fetchMock = serve({
      "GET /api/intent": () => respond(200, { entries: [firstEntry], activeWords: 4, condenseAt: 1000 }),
      "POST /api/intent": (init) =>
        respond(201, { id: 2, text: bodyOf(init).text, kind: "entry", createdAt: "2026-10-07T09:00:00Z" }),
    });
    render(<IntentLog />);
    expect(await screen.findByText("Forward-deployed roles with customers")).toBeInTheDocument();
    expect(screen.getByText("4 of 1000 words")).toBeInTheDocument();

    const box = screen.getByLabelText("Add to what you are looking for");
    const add = screen.getByRole("button", { name: "Add" });
    expect(add).toBeDisabled();
    fireEvent.change(box, { target: { value: "Not adtech, ever" } });
    fireEvent.click(add);

    expect(await screen.findByText("Not adtech, ever")).toBeInTheDocument();
    expect(box).toHaveValue("");
    expect(screen.getByText("7 of 1000 words")).toBeInTheDocument();
    await openPast();
    const items = screen.getAllByRole("listitem");
    expect(items[0]).toHaveTextContent("Forward-deployed roles with customers");
    expect(items[1]).toHaveTextContent("Not adtech, ever");
    expect(bodyOf(fetchMock.mock.calls[1][1])).toEqual({ text: "Not adtech, ever" });
  });

  it("removes an entry", async () => {
    serve({
      "GET /api/intent": () => respond(200, { entries: [firstEntry], activeWords: 4, condenseAt: 1000 }),
      "DELETE /api/intent/1": () => respond(204),
    });
    render(<IntentLog />);
    await openPast();
    fireEvent.click(await screen.findByRole("button", { name: "Remove entry" }));
    await waitFor(() =>
      expect(screen.queryByText("Forward-deployed roles with customers")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("0 of 1000 words")).toBeInTheDocument();
  });

  it("says a long log is ready to condense", async () => {
    serve({
      "GET /api/intent": () => respond(200, { entries: [firstEntry], activeWords: 1000, condenseAt: 1000 }),
    });
    render(<IntentLog />);
    expect(await screen.findByText("Long enough to condense into one note.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Condense" })).toHaveClass("primary-action");
  });

  it("offers no condense for a single short entry, a quiet one for two", async () => {
    serve({
      "GET /api/intent": () => respond(200, { entries: [firstEntry], activeWords: 4, condenseAt: 1000 }),
      "POST /api/intent": () => respond(201, secondEntry),
    });
    render(<IntentLog />);
    await screen.findByText(firstEntry.text);
    expect(screen.queryByRole("button", { name: "Condense" })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Add to what you are looking for"), {
      target: { value: secondEntry.text },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("button", { name: "Condense" })).toHaveClass("ghost-action");
  });

  it("drafts a condensed note, lets it be edited, and approves it", async () => {
    const note = {
      id: 3,
      text: "I want forward-deployed roles, never adtech.",
      kind: "condensed",
      createdAt: "2026-10-07T10:00:00Z",
    };
    const fetchMock = serve({
      "GET /api/intent": () =>
        respond(200, { entries: [firstEntry, secondEntry], activeWords: 7, condenseAt: 1000 }),
      "POST /api/intent/condense": () => respond(200, { draft: "I want forward-deployed roles." }),
      "POST /api/intent/condense/approve": () =>
        respond(201, { entries: [note], activeWords: 7, condenseAt: 1000 }),
    });
    render(<IntentLog />);
    fireEvent.click(await screen.findByRole("button", { name: "Condense" }));

    const box = await screen.findByLabelText("Condensed note");
    expect(box).toHaveValue("I want forward-deployed roles.");
    expect(screen.queryByRole("button", { name: "Condense" })).not.toBeInTheDocument();
    fireEvent.change(box, { target: { value: note.text } });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    // Wait for the draft to close first: until then the edited text is also in the box.
    await waitFor(() => expect(screen.queryByLabelText("Condensed note")).not.toBeInTheDocument());
    expect(screen.getByText(note.text)).toBeInTheDocument();
    expect(screen.queryByText(firstEntry.text)).not.toBeInTheDocument();
    expect(screen.queryByText(secondEntry.text)).not.toBeInTheDocument();
    await openPast();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText(/condensed/)).toBeInTheDocument();
    const approveCall = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/approve"));
    expect(bodyOf(approveCall?.[1])).toEqual({ text: note.text });
  });

  it("cancels a draft without touching the log", async () => {
    const fetchMock = serve({
      "GET /api/intent": () =>
        respond(200, { entries: [firstEntry, secondEntry], activeWords: 7, condenseAt: 1000 }),
      "POST /api/intent/condense": () => respond(200, { draft: "A draft." }),
    });
    render(<IntentLog />);
    fireEvent.click(await screen.findByRole("button", { name: "Condense" }));
    await screen.findByLabelText("Condensed note");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("Condensed note")).not.toBeInTheDocument();
    await openPast();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows the model's failure inline and keeps the entries", async () => {
    serve({
      "GET /api/intent": () =>
        respond(200, { entries: [firstEntry, secondEntry], activeWords: 7, condenseAt: 1000 }),
      "POST /api/intent/condense": () =>
        respond(502, { detail: { errors: { model: "Could not draft a condensed note: backend down" } } }),
    });
    render(<IntentLog />);
    fireEvent.click(await screen.findByRole("button", { name: "Condense" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not draft a condensed note: backend down",
    );
    expect(screen.queryByLabelText("Condensed note")).not.toBeInTheDocument();
    await openPast();
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
  });

  it("shows a refused approval inline and keeps the draft open", async () => {
    serve({
      "GET /api/intent": () =>
        respond(200, { entries: [firstEntry, secondEntry], activeWords: 7, condenseAt: 1000 }),
      "POST /api/intent/condense": () => respond(200, { draft: "A draft." }),
      "POST /api/intent/condense/approve": () =>
        respond(422, { detail: { errors: { text: "keep the note under 4000 characters" } } }),
    });
    render(<IntentLog />);
    fireEvent.click(await screen.findByRole("button", { name: "Condense" }));
    await screen.findByLabelText("Condensed note");
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("keep the note under 4000 characters");
    expect(screen.getByLabelText("Condensed note")).toHaveValue("A draft.");
  });

  it("shows the server's message when an entry is refused", async () => {
    serve({
      "GET /api/intent": () => respond(200, { entries: [], activeWords: 0, condenseAt: 1000 }),
      "POST /api/intent": () => respond(422, { detail: { errors: { text: "too long" } } }),
    });
    render(<IntentLog />);
    fireEvent.change(await screen.findByLabelText("Add to what you are looking for"), {
      target: { value: "x" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("too long");
  });
});

describe("sample roles", () => {
  beforeEach(() => vi.unstubAllGlobals());

  const role = {
    id: 7,
    company: "Taktile",
    title: "Forward Deployed Engineer",
    source: "ashby",
    url: "https://jobs.ashbyhq.com/taktile/594e",
    addedAt: "2026-10-07T09:00:00Z",
  };

  it("adds a role from a good link", async () => {
    const fetchMock = serve({
      "GET /api/sample-roles": () => respond(200, { roles: [] }),
      "POST /api/sample-roles": () => respond(201, role),
    });
    render(<SampleRoles />);
    expect(await screen.findByRole("heading", { name: "Sample roles you like" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Job link"), { target: { value: role.url } });
    fireEvent.click(screen.getByRole("button", { name: "Add role" }));

    const link = await screen.findByRole("link", { name: "Forward Deployed Engineer" });
    expect(link).toHaveAttribute("href", role.url);
    expect(link.closest("li")).toHaveTextContent("Taktile — Forward Deployed Engineer");
    expect(bodyOf(fetchMock.mock.calls[1][1])).toEqual({ url: role.url });
    expect(screen.getByLabelText("Job link")).toHaveValue("");
  });

  it("offers a paste form for a link it cannot read, and lists the pasted role", async () => {
    const fetchMock = serve({
      "GET /api/sample-roles": () => respond(200, { roles: [] }),
      "POST /api/sample-roles": (init) =>
        "url" in bodyOf(init)
          ? respond(422, {
              detail: { errors: { url: "That posting is closed. Paste its text." }, needsText: true },
            })
          : respond(201, {
              id: 8,
              company: "Acme",
              title: "Solutions Engineer",
              source: "manual",
              url: null,
              addedAt: "2026-10-07T09:00:00Z",
            }),
    });
    render(<SampleRoles />);
    fireEvent.change(await screen.findByLabelText("Job link"), {
      target: { value: "https://example.com/closed" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add role" }));

    expect(await screen.findByText("That posting is closed. Paste its text.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Job title"), { target: { value: "Solutions Engineer" } });
    fireEvent.change(screen.getByLabelText("Company (optional)"), { target: { value: "Acme" } });
    fireEvent.change(screen.getByLabelText("Job text"), { target: { value: "You will work with…" } });
    fireEvent.click(screen.getByRole("button", { name: "Add pasted role" }));

    const item = (await screen.findByText(/Acme — Solutions Engineer/)).closest("li") as HTMLElement;
    expect(within(item).queryByRole("link")).not.toBeInTheDocument();
    expect(bodyOf(fetchMock.mock.calls[2][1])).toEqual({
      title: "Solutions Engineer",
      text: "You will work with…",
      company: "Acme",
    });
    expect(screen.queryByLabelText("Job text")).not.toBeInTheDocument();
  });

  it("removes a role", async () => {
    serve({
      "GET /api/sample-roles": () => respond(200, { roles: [role] }),
      "DELETE /api/sample-roles/7": () => respond(204),
    });
    render(<SampleRoles />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove sample role" }));
    await waitFor(() =>
      expect(screen.queryByText(/Forward Deployed Engineer/)).not.toBeInTheDocument(),
    );
  });
});
