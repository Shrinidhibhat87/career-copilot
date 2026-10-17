import { describe, expect, it } from "vitest";

import { euros } from "@/lib/money";

describe("euros", () => {
  it("shows cents, not fractions of a cent", () => {
    expect(euros(1)).toBe("€1.00");
    expect(euros(0.5)).toBe("€0.50");
    expect(euros(3.6)).toBe("€3.60");
    expect(euros(0.1055)).toBe("€0.11");
  });

  it("does not round a real spend down to nothing", () => {
    expect(euros(0)).toBe("€0.00");
    expect(euros(0.0004)).toBe("<€0.01");
  });
});
