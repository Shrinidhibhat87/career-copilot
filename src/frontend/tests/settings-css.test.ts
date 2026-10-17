import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// jsdom does not apply stylesheets, so the run-history tints are checked in the source.
const css = readFileSync(join(process.cwd(), "app/globals.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const root = css.match(/:root\s*{([^}]*)}/)?.[1] ?? "";

function token(name: string): string {
  return root.match(new RegExp(`${name}:\\s*(#[0-9a-f]{6})`, "i"))?.[1] ?? "";
}

/** The background a `.run-history` row in this state gets, or "" for none. */
function tintFor(state: string): string {
  for (const [, selectors, body] of css.matchAll(/([^{}]+){([^}]*)}/g)) {
    const matches = selectors.split(",").some(
      (selector) => selector.trim() === `.run-history li[data-state="${state}"]`,
    );
    if (matches) return body.match(/background:\s*([^;]+);/)?.[1].trim() ?? "";
  }
  return "";
}

function luminance(hex: string): number {
  const channels = [1, 3, 5].map((index) => parseInt(hex.slice(index, index + 2), 16) / 255);
  const [r, g, b] = channels.map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}

/** The declarations of the first rule whose selector list is exactly `selector`. */
function rule(selector: string): string {
  for (const [, selectors, body] of css.matchAll(/([^{}]+){([^}]*)}/g)) {
    if (selectors.trim() === selector) return body;
  }
  return "";
}

describe("settings layout", () => {
  it("centres one column that the title and every card share", () => {
    expect(rule(".settings-page > *")).toMatch(/max-width:\s*1100px/);
    expect(rule(".settings-page > *")).toMatch(/margin-inline:\s*auto/);
    expect(rule(".settings-page > h1")).toMatch(/text-align:\s*center/);
  });

  it("centres the title on the full page, not inside the left of a capped column", () => {
    const title = rule(".settings-page > h1");
    expect(title).toMatch(/max-width:\s*none/);
    expect(title).not.toMatch(/margin:\s*0 0 /);
  });
});

describe("run history colours", () => {
  it("defines the ok and bad tokens", () => {
    expect(token("--color-ok-bg")).toBe("#e7f1e4");
    expect(token("--color-bad-bg")).toBe("#f6e3e0");
  });

  it("tints finished rows by outcome and leaves live ones neutral", () => {
    expect(tintFor("succeeded")).toBe("var(--color-ok-bg)");
    expect(tintFor("failed")).toBe("var(--color-bad-bg)");
    expect(tintFor("aborted")).toBe("var(--color-bad-bg)");
    expect(tintFor("running")).toBe("");
    expect(tintFor("syncing")).toBe("");
  });

  it("keeps ink readable on both tints", () => {
    const ink = token("--color-ink");
    expect(contrast(ink, token("--color-ok-bg"))).toBeGreaterThanOrEqual(4.5);
    expect(contrast(ink, token("--color-bad-bg"))).toBeGreaterThanOrEqual(4.5);
  });
});
