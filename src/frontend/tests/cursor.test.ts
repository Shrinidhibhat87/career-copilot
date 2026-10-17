import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(resolve(process.cwd(), "app/globals.css"), "utf8");

/** Every rule block as [selector, declarations]. The stylesheet has no nested blocks
 * outside @media, which this simple walk flattens too. */
function rules(source: string): [string, string][] {
  return [...source.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => [
    m[1].trim(),
    m[2],
  ]);
}

describe("cursors", () => {
  it("never gives a disabled control the wait cursor", () => {
    const offenders = rules(css).filter(
      ([selector, body]) => /:disabled/.test(selector) && /cursor:\s*wait/.test(body),
    );
    expect(offenders).toEqual([]);
  });

  it("uses the wait cursor only for a request in flight", () => {
    const waits = rules(css).filter(([, body]) => /cursor:\s*wait/.test(body));
    expect(waits.length).toBeGreaterThan(0);
    for (const [selector] of waits) {
      expect(selector).toContain('[aria-busy="true"]');
    }
  });

  it("marks a disabled button not-allowed", () => {
    const disabled = rules(css).filter(([selector, body]) =>
      /:disabled/.test(selector) && /cursor:\s*not-allowed/.test(body),
    );
    expect(disabled.length).toBeGreaterThan(0);
  });
});
