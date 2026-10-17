import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PrivacyBadge } from "@/components/privacy-badge";
import type { ResumePrivacy } from "@/lib/api";

const PREVIEW =
  "Data & AI Engineer · <|PERSON_1|> · Berlin\n<|EMAIL_1|> · <|PHONE_1|>\nBosch Research — segmentation, 2019–2021";

const enabled: ResumePrivacy = {
  enabled: true,
  counts: { PERSON: 3, EMAIL: 1, PHONE: 1 },
  redactedPreview: PREVIEW,
  ownerAnchor: "confirmed",
  unavailableReason: null,
};

describe("privacy badge", () => {
  it("names how many values of each kind are replaced", () => {
    render(<PrivacyBadge privacy={enabled} />);
    expect(
      screen.getByText("3 names, 1 email address and 1 phone number will be replaced before any model call."),
    ).toBeInTheDocument();
  });

  it("claims pseudonymity, not anonymity, and says what still goes out", () => {
    render(<PrivacyBadge privacy={enabled} />);
    const note = screen.getByText(/pseudonymous, not anonymous/);
    expect(note).toHaveTextContent("Employers, dates and skills are still sent");
    // The only permitted use of the word is the denial itself.
    expect(document.body.textContent).not.toMatch(/(?<!not )anonymous/);
  });

  it("discloses the redacted text inside the Privacy details panel", () => {
    render(<PrivacyBadge privacy={enabled} />);
    expect(screen.getByText("Privacy details")).toBeInTheDocument();
    expect(screen.getByText("Redacted text")).toBeInTheDocument();
    // Not "what gets sent": the panel says what it is, and what it is not.
    expect(screen.getByText(/not a live transcript/)).toBeInTheDocument();
    for (const token of ["<|PERSON_1|>", "<|EMAIL_1|>", "<|PHONE_1|>"]) {
      expect(screen.getByText(token)).toBeInTheDocument();
    }
    expect(screen.getByText(/Bosch Research/)).toBeInTheDocument();
  });

  it("marks each placeholder so it stands out from the surviving text", () => {
    render(<PrivacyBadge privacy={enabled} />);
    const marks = document.querySelectorAll("mark.redaction-token");
    expect([...marks].map((mark) => mark.textContent)).toEqual([
      "<|PERSON_1|>",
      "<|EMAIL_1|>",
      "<|PHONE_1|>",
    ]);
  });

  it("puts the disclosure behind a native details/summary, so it is keyboard operable", () => {
    render(<PrivacyBadge privacy={enabled} />);
    // jsdom implements neither summary focus nor details toggling, so this check is
    // structural rather than behavioural: a native <summary> is tab-reachable and
    // Enter-operable in every browser, and a div with an onClick is not. What the test can
    // guard is that the control stays a summary and picks up no tabindex override.
    const panel = document.querySelector("details");
    const summary = panel?.querySelector(":scope > summary");
    expect(summary?.textContent).toBe("Privacy details");
    expect(summary?.getAttribute("tabindex")).toBeNull();
    expect(panel?.textContent).toContain("<|PERSON_1|>");
  });

  it("keeps the notices and the preview in one closed details, headline outside", () => {
    render(<PrivacyBadge privacy={enabled} />);
    const panels = document.querySelectorAll("details");
    expect(panels).toHaveLength(1);
    const panel = panels[0];
    expect(panel).not.toHaveAttribute("open");
    expect(panel).toContainElement(screen.getByText(/pseudonymous, not anonymous/));
    expect(panel).toContainElement(screen.getByText(/detected from your resume/));
    expect(panel).toContainElement(screen.getByText(/not a live transcript/));
    const headline = screen.getByText(/will be replaced before any model call/);
    expect(panel).not.toContainElement(headline);
  });

  it("says redaction is off rather than disappearing", () => {
    render(
      <PrivacyBadge
        privacy={{
          enabled: false,
          counts: {},
          redactedPreview: null,
          ownerAnchor: "none",
          unavailableReason: null,
        }}
      />,
    );
    expect(screen.getByText(/Redaction is off/)).toHaveTextContent(
      "Your resume goes to the model provider as it is",
    );
    expect(screen.queryByText("Redacted text")).not.toBeInTheDocument();
    expect(screen.queryByText(/will be replaced/)).not.toBeInTheDocument();
  });

  it("says there is no preview when scanning this file failed", () => {
    render(
      <PrivacyBadge
        privacy={{
          enabled: true,
          counts: {},
          redactedPreview: null,
          ownerAnchor: "none",
          unavailableReason: null,
        }}
      />,
    );
    expect(screen.getByText(/could not be scanned/)).toHaveTextContent(
      "a scoring call fails rather than going out unredacted",
    );
    // A failure must not read as "nothing to redact".
    expect(screen.queryByText(/No name, email address or phone number/)).not.toBeInTheDocument();
    expect(screen.queryByText("Redacted text")).not.toBeInTheDocument();
  });

  it("names why the scan failed, so the remedy is knowable", () => {
    render(
      <PrivacyBadge
        privacy={{
          enabled: true,
          counts: {},
          redactedPreview: null,
          ownerAnchor: "none",
          unavailableReason: "the spaCy model 'en_core_web_lg' is not installed on this machine",
        }}
      />,
    );
    expect(screen.getByText(/could not be scanned/)).toBeInTheDocument();
    expect(screen.getByText(/en_core_web_lg/)).toBeInTheDocument();
  });

  it("does not claim replacements when the file held none", () => {
    render(
      <PrivacyBadge
        privacy={{
          enabled: true,
          counts: {},
          redactedPreview: "Python engineer",
          ownerAnchor: "none",
          unavailableReason: null,
        }}
      />,
    );
    expect(
      screen.getByText("No name, email address or phone number was found in this file."),
    ).toBeInTheDocument();
    expect(screen.getByText("Redacted text")).toBeInTheDocument();
  });
});

describe("owner anchor disclosure", () => {
  it("says the identity was detected from the resume when it is confirmed", () => {
    render(<PrivacyBadge privacy={enabled} />);
    expect(
      screen.getByText(/detected from your resume and replaced everywhere they appear/),
    ).toBeInTheDocument();
  });

  it("discloses the weaker level so a derivation mistake is visible", () => {
    render(<PrivacyBadge privacy={{ ...enabled, ownerAnchor: "likely" }} />);
    const note = screen.getByText(/could not be cross-checked/);
    expect(note).toHaveTextContent("check the preview");
  });

  it("tags the summary line with Check name only when the anchor is likely", () => {
    const { rerender } = render(<PrivacyBadge privacy={{ ...enabled, ownerAnchor: "likely" }} />);
    expect(document.querySelector("details > summary")).toHaveTextContent("Check name");
    for (const ownerAnchor of ["confirmed", "none"] as const) {
      rerender(<PrivacyBadge privacy={{ ...enabled, ownerAnchor }} />);
      expect(screen.queryByText("Check name")).not.toBeInTheDocument();
    }
  });

  it("says name redaction fell back to the model when no owner was confirmed", () => {
    render(<PrivacyBadge privacy={{ ...enabled, ownerAnchor: "none" }} />);
    expect(screen.getByText(/No owner name could be confirmed/)).toBeInTheDocument();
    // The contact values still anchor at `none`, and the copy must not imply otherwise.
    expect(screen.getByText(/email address and phone number are still replaced/)).toBeInTheDocument();
  });
});
