import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { InfoTip } from "@/components/info-tip";

function open() {
  fireEvent.click(screen.getByRole("button", { name: "About the thing" }));
}

describe("info tip", () => {
  it("reveals the explanation on click and hides it again", () => {
    render(<InfoTip label="About the thing">Why this field exists.</InfoTip>);
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    open();
    expect(screen.getByRole("note")).toHaveTextContent("Why this field exists.");
    open();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("closes on a press anywhere else, not only on its own button", () => {
    render(
      <>
        <InfoTip label="About the thing">Why this field exists.</InfoTip>
        <button type="button">Somewhere else</button>
      </>,
    );
    open();
    fireEvent.pointerDown(screen.getByRole("button", { name: "Somewhere else" }));
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("stays open when the press lands inside its own text", () => {
    render(<InfoTip label="About the thing">Why this field exists.</InfoTip>);
    open();
    fireEvent.pointerDown(screen.getByRole("note"));
    expect(screen.getByRole("note")).toBeInTheDocument();
  });

  it("closes on Escape", () => {
    render(<InfoTip label="About the thing">Why this field exists.</InfoTip>);
    open();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
  });

  it("leaves only the second tip open when a second one is pressed", () => {
    render(
      <>
        <InfoTip label="About the thing">First note.</InfoTip>
        <InfoTip label="About the other thing">Second note.</InfoTip>
      </>,
    );
    open();
    const second = screen.getByRole("button", { name: "About the other thing" });
    fireEvent.pointerDown(second);
    fireEvent.click(second);
    expect(screen.getAllByRole("note")).toHaveLength(1);
    expect(screen.getByRole("note")).toHaveTextContent("Second note.");
  });
});
