import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const replace = vi.fn();
vi.mock("next/navigation", () => ({ useRouter: () => ({ replace }) }));

import LegacyReviewPage from "@/app/review/page";

describe("the old /review route", () => {
  it("replaces itself with /recommendations", () => {
    render(<LegacyReviewPage />);
    expect(replace).toHaveBeenCalledWith("/recommendations");
  });
});
