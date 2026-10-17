import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthGuard } from "@/components/auth-guard";

describe("auth guard", () => {
  const original = window.fetch;
  afterEach(() => {
    window.fetch = original;
    vi.restoreAllMocks();
  });

  function setup(status: number) {
    const assign = vi.fn();
    vi.spyOn(window, "location", "get").mockReturnValue({
      pathname: "/recommendations",
      assign,
    } as unknown as Location);
    window.fetch = vi.fn().mockResolvedValue(new Response("{}", { status }));
    render(<AuthGuard />);
    return assign;
  }

  it("sends the browser to the sign-in page on a 401 from the API", async () => {
    const assign = setup(401);
    await window.fetch("/api/runs");
    expect(assign).toHaveBeenCalledWith("/");
  });

  it("leaves the sign-in calls alone: a wrong passcode is a 401 too", async () => {
    const assign = setup(401);
    await window.fetch("/api/auth/login");
    expect(assign).not.toHaveBeenCalled();
  });

  it("does nothing for an ordinary answer", async () => {
    const assign = setup(200);
    await window.fetch("/api/runs");
    expect(assign).not.toHaveBeenCalled();
  });
});
