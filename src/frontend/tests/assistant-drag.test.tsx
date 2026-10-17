import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { AssistantChat } from "@/components/assistant-chat";

function chat() {
  return render(
    <AssistantChat
      knownIds={[]}
      view={{ limit: 10, includeBelow: false }}
      onActions={() => undefined}
      onRefetch={async () => []}
    />,
  );
}

describe("moving the Copilot button", () => {
  beforeEach(() => window.localStorage.clear());

  it("drags to a new place, and a drag is not a click", () => {
    chat();
    const button = screen.getByRole("button", { name: "Open Copilot chat" });
    fireEvent.pointerDown(button, { button: 0, clientX: 500, clientY: 500 });
    fireEvent.pointerMove(button, { clientX: 400, clientY: 450 });
    fireEvent.pointerUp(button, { clientX: 400, clientY: 450 });
    fireEvent.click(button);

    expect(button.style.getPropertyValue("--copilot-right")).toBe("124px");
    expect(button.style.getPropertyValue("--copilot-bottom")).toBe("74px");
    expect(
      screen.queryByRole("region", { name: "Copilot chat" }),
    ).not.toBeInTheDocument();
    expect(window.localStorage.getItem("copilot-position")).toBe(
      JSON.stringify({ right: 124, bottom: 74 }),
    );
  });

  it("opens on a plain click", () => {
    chat();
    const button = screen.getByRole("button", { name: "Open Copilot chat" });
    fireEvent.pointerDown(button, { button: 0, clientX: 10, clientY: 10 });
    fireEvent.pointerUp(button, { clientX: 11, clientY: 10 });
    fireEvent.click(button);
    expect(
      screen.getByRole("region", { name: "Copilot chat" }),
    ).toBeInTheDocument();
  });

  it("comes back where it was dropped after a reload", () => {
    window.localStorage.setItem(
      "copilot-position",
      JSON.stringify({ right: 300, bottom: 200 }),
    );
    chat();
    const button = screen.getByRole("button", { name: "Open Copilot chat" });
    expect(button.style.getPropertyValue("--copilot-right")).toBe("300px");
    expect(button.style.getPropertyValue("--copilot-bottom")).toBe("200px");
  });

  it("starts at the corner when the saved position is junk", () => {
    window.localStorage.setItem("copilot-position", "not json");
    chat();
    const button = screen.getByRole("button", { name: "Open Copilot chat" });
    expect(button.style.getPropertyValue("--copilot-right")).toBe("24px");
  });
});
