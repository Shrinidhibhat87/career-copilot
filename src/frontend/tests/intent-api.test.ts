import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  addIntent,
  approveCondensed,
  CONDENSE_FAILED,
  condenseIntent,
  addSampleRoleUrl,
  getSampleRoles,
  INTENT_UNREACHABLE,
  NeedsTextError,
  ValidationError,
} from "@/lib/api";

function respond(status: number, body: unknown) {
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

describe("intent and sample-role client", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("asks for pasted text when the server cannot use a link", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        respond(422, {
          detail: { errors: { url: "That posting is closed. Paste its text instead." }, needsText: true },
        }),
      ),
    );
    const attempt = addSampleRoleUrl("https://boards.greenhouse.io/acme/jobs/1");
    await expect(attempt).rejects.toBeInstanceOf(NeedsTextError);
    await expect(attempt).rejects.toThrow("That posting is closed. Paste its text instead.");
  });

  it("unwraps the list of sample roles", async () => {
    const role = {
      id: 1,
      company: "Acme",
      title: "Forward Deployed Engineer",
      source: "greenhouse",
      url: "https://boards.greenhouse.io/acme/jobs/1",
      addedAt: "2026-10-07T10:00:00Z",
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond(200, { roles: [role] })));
    await expect(getSampleRoles()).resolves.toEqual([role]);
  });

  it("refuses a blank intent entry with the server's field message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(respond(422, { detail: { errors: { text: "write something first" } } })),
    );
    const attempt = addIntent("   ");
    await expect(attempt).rejects.toBeInstanceOf(ValidationError);
    await expect(attempt).rejects.toMatchObject({ errors: { text: "write something first" } });
  });

  it("unwraps a condensed draft", async () => {
    const fetchMock = vi.fn().mockResolvedValue(respond(200, { draft: "One note." }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(condenseIntent()).resolves.toBe("One note.");
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/api\/intent\/condense$/);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: "POST" });
  });

  it("surfaces the model's reason when condensing fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(respond(502, { detail: { errors: { model: "backend down" } } })),
    );
    await expect(condenseIntent()).rejects.toThrow("backend down");
  });

  it("falls back to a fixed message when a 502 carries no reason", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(respond(502, {})));
    await expect(condenseIntent()).rejects.toThrow(CONDENSE_FAILED);
  });

  it("posts the approved note and returns the new log", async () => {
    const log = {
      entries: [{ id: 3, text: "One note.", kind: "condensed", createdAt: "2026-10-07T10:00:00Z" }],
      activeWords: 2,
      condenseAt: 1000,
    };
    const fetchMock = vi.fn().mockResolvedValue(respond(201, log));
    vi.stubGlobal("fetch", fetchMock);
    await expect(approveCondensed("One note.")).resolves.toEqual(log);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/api\/intent\/condense\/approve$/);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({ text: "One note." });
  });

  it("names the service when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(getSampleRoles()).rejects.toThrow(INTENT_UNREACHABLE);
  });
});
