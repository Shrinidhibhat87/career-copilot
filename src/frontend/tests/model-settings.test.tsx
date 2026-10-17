import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { keyTestLine, ModelSettings, modelPrivacy } from "@/components/model-settings";
import type { ModelSettingsView } from "@/lib/api";

function view(overrides: Partial<ModelSettingsView> = {}): ModelSettingsView {
  return {
    settings: { version: 0, preset: null, overrides: {}, cloudConsent: false, savedAt: null },
    profile: { name: "nvidia-vllm-awq", source: "auto", reason: "auto: NVIDIA GPU found" },
    envProfile: null,
    roles: [
      { role: "triage", kind: "vllm", model: "qwen3-4b-instruct", source: "config", local: true, needs: "response_format" },
      { role: "match", kind: "vllm", model: "qwen3-4b-instruct", source: "config", local: true, needs: "response_format" },
    ],
    backends: ["ollama", "openrouter", "vllm"],
    key: { set: false, source: "none", tail: null, keychain: true },
    budget: { maxEurPerRun: 1, lastRunEur: null },
    ...overrides,
  };
}

function response(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

async function open() {
  render(<ModelSettings />);
  fireEvent.click(await screen.findByRole("button", { name: "Models" }));
}

describe("ModelSettings", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("labels the privacy of the roles in force", () => {
    expect(modelPrivacy(view())).toBe("Local only");
    const hybrid = view();
    hybrid.roles[1] = { ...hybrid.roles[1], kind: "openrouter", local: false };
    expect(modelPrivacy(hybrid)).toBe("Cloud: 1 role (redacted)");
  });

  it("asks for consent once when a cloud preset is saved, then saves with it", async () => {
    const bodies: unknown[] = [];
    const saved = view({
      settings: { version: 1, preset: "hybrid", overrides: {}, cloudConsent: true, savedAt: null },
    });
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "PUT") {
        const body = JSON.parse(String(init.body));
        bodies.push(body);
        return body.cloudConsent
          ? response(saved)
          : response({ detail: { errors: { consent: "Accept the cloud notice first" } } }, 428);
      }
      return response(view({ key: { set: true, source: "env", tail: "1234", keychain: true } }));
    }));
    await open();
    fireEvent.click(screen.getByRole("radio", { name: /Hybrid/ }));
    expect(screen.getByText(/Key ending …1234 is in use, from .env/)).toBeInTheDocument();
    expect(screen.queryByLabelText("OpenRouter key")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Use Hybrid" }));
    expect(await screen.findByRole("alertdialog", { name: "Cloud notice" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Accept and save" }));
    expect(await screen.findByText("Saved. The next run uses these models.")).toBeInTheDocument();
    expect(bodies).toEqual([
      { preset: "hybrid", overrides: {} },
      { preset: "hybrid", overrides: {}, cloudConsent: true },
    ]);
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("shows the server's reason when a cloud preset has no key", async () => {
    vi.stubGlobal("fetch", vi.fn(async (_url: string, init?: RequestInit) =>
      init?.method === "PUT"
        ? response({ detail: { errors: { key: "Add an OpenRouter key before moving a role to the cloud." } } }, 409)
        : response(view())));
    await open();
    fireEvent.click(screen.getByRole("radio", { name: /Cloud/ }));
    fireEvent.click(screen.getByRole("button", { name: "Use Cloud" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Add an OpenRouter key");
  });

  it("marks an overridden role and tests a key without echoing it", async () => {
    const overridden = view({
      settings: { version: 2, preset: "local", overrides: { match: { backend: "openrouter", model: "x/y" } }, cloudConsent: true, savedAt: null },
    });
    overridden.roles[1] = { ...overridden.roles[1], kind: "openrouter", model: "x/y", source: "override", local: false };
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      String(url).includes("openrouter-key/test")
        ? response({ valid: true, limit: null, usage: 0.25, detail: "" })
        : response(overridden)));
    await open();
    expect(screen.getByText("override")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("OpenRouter key"), { target: { value: "sk-or-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Test" }));
    await waitFor(() => expect(screen.getByText(/The key works/)).toBeInTheDocument());
    expect(screen.queryByText(/sk-or-secret/)).not.toBeInTheDocument();
  });

  it("words a key test so account credit never reads as the test's cost", () => {
    expect(keyTestLine({ valid: true, limit: 5, usage: 2.42 })).toBe(
      "The key works. This key has used $2.42 of credit so far out of its $5.00 limit. Testing it cost nothing.",
    );
    expect(keyTestLine({ valid: false, limit: null, usage: null })).toBe("OpenRouter rejected this key.");
  });

  it("saves a key, acknowledges it and hides the input", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).endsWith("openrouter-key") && init?.method === "PUT") {
        return response({ set: true, source: "session", tail: "abcd", keychain: false });
      }
      return response(view({ settings: { version: 1, preset: "hybrid", overrides: {}, cloudConsent: true, savedAt: null } }));
    }));
    await open();
    fireEvent.change(screen.getByLabelText("OpenRouter key"), { target: { value: "sk-or-v1-abcd" } });
    fireEvent.click(screen.getByRole("button", { name: "Test and save" }));
    expect(await screen.findByText(/Key saved and hidden/)).toBeInTheDocument();
    expect(screen.getByText(/Key ending …abcd is in use, kept for this session only/)).toBeInTheDocument();
    expect(screen.getByText("Manage key")).toBeInTheDocument();
  });

  it("hides the key panel while everything is local", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(view())));
    await open();
    expect(screen.queryByText("OpenRouter key")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("radio", { name: /Hybrid/ }));
    expect(screen.getByLabelText("OpenRouter key")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("OpenRouter key")).not.toBeInTheDocument();
  });

  it("warns that an env profile overrides a saved preset", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response(view({ envProfile: "none" }))));
    await open();
    expect(screen.getByRole("note")).toHaveTextContent("CAREER_COPILOT_PROFILE=none");
  });
});
