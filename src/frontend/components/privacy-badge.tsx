import type { OwnerAnchor, ResumePrivacy } from "@/lib/api";

// Singular/plural wording per category, so the badge reads as a sentence rather than as
// `PERSON: 3`. A category the API adds later falls back to its own name instead of being
// dropped: an undisclosed replacement is worse than an ugly one.
const LABELS: Record<string, [string, string]> = {
  PERSON: ["name", "names"],
  EMAIL: ["email address", "email addresses"],
  PHONE: ["phone number", "phone numbers"],
  ADDRESS: ["street address", "street addresses"],
  URL: ["link", "links"],
  IBAN: ["IBAN", "IBANs"],
  CREDIT_CARD: ["card number", "card numbers"],
  IP: ["IP address", "IP addresses"],
};

const PLACEHOLDER = /(<\|[A-Z_]+_\d+\|>)/g;
// A separate, non-global copy: `test` on a /g regex advances `lastIndex` and would skip
// every other placeholder.
const IS_PLACEHOLDER = /^<\|[A-Z_]+_\d+\|>$/;

function phrase(category: string, count: number): string {
  const [one, many] = LABELS[category] ?? [category, category];
  return `${count} ${count === 1 ? one : many}`;
}

/** "3 names, 1 email address and 1 phone number" — an Oxford-comma-free list. */
function listCounts(counts: Record<string, number>): string {
  const parts = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([category, count]) => phrase(category, count));
  if (parts.length <= 1) return parts[0] ?? "";
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}

// What the anchor actually did, per level. `likely` and `none` both disclose the weaker
// outcome rather than hiding it: that is how a derivation mistake becomes visible to the
// user without them having to author an identity file first.
const ANCHOR_COPY: Record<OwnerAnchor, string> = {
  confirmed:
    "Your name, email address and phone number were detected from your resume and replaced everywhere they appear, not only where the model happened to notice them.",
  likely:
    "Your name was read from your resume but could not be cross-checked against your email address or filename, so check the preview below to confirm the right name was picked.",
  none:
    "No owner name could be confirmed in this file, so name redaction falls back to what the model detects. Your email address and phone number are still replaced everywhere they appear.",
};

/** The redacted text, with every `<|PERSON_1|>` marked so it is visible at a glance. */
function highlight(preview: string) {
  return preview.split(PLACEHOLDER).map((piece, index) =>
    IS_PLACEHOLDER.test(piece) ? (
      <mark className="redaction-token" key={index}>
        {piece}
      </mark>
    ) : (
      <span key={index}>{piece}</span>
    ),
  );
}

/**
 * Discloses what the redaction boundary does to this resume (design doc §12).
 *
 * Three states, none of which hides the badge: redaction on with a preview, redaction on
 * with no preview (detection failed on this file), and redaction switched off. The copy
 * claims pseudonymity and never anonymity — employers, dates and skills stay in the prompt
 * on purpose (§3), and the preview in the panel is what lets the user check the claim.
 */
export function PrivacyBadge({ privacy }: { privacy: ResumePrivacy }) {
  const replaced = listCounts(privacy.counts);

  return (
    <section className="privacy-badge" aria-label="Privacy">
      <p className="privacy-label">PRIVACY</p>
      {!privacy.enabled ? (
        <p className="privacy-headline">
          Redaction is off. Your resume goes to the model provider as it is, including your
          name, email address and phone number. Set <code>pii.enabled</code> in{" "}
          <code>config.yaml</code> to turn it back on.
        </p>
      ) : privacy.redactedPreview === null ? (
        <>
          <p className="privacy-headline">
            Redaction is on, but this file could not be scanned, so there is no preview to show.
            Text that cannot be redacted is not sent: a scoring call fails rather than going out
            unredacted.
          </p>
          {/* The reason, when the server knows one. A failure class or a named missing
              model — enough to act on, and never a value out of the resume. */}
          {privacy.unavailableReason ? (
            <p className="privacy-note">Reason: {privacy.unavailableReason}.</p>
          ) : null}
        </>
      ) : (
        <>
          <p className="privacy-headline">
            {replaced
              ? `${replaced} will be replaced before any model call.`
              : "No name, email address or phone number was found in this file."}
          </p>
          <details className="privacy-panel">
            <summary>
              Privacy details
              {privacy.ownerAnchor === "likely" ? (
                <span className="privacy-tag">Check name</span>
              ) : null}
            </summary>
            <p className="privacy-note">{ANCHOR_COPY[privacy.ownerAnchor]}</p>
            <p className="privacy-note">
              Your name, email, phone and address are replaced. Employers, dates and skills are
              still sent, so the resume the provider sees is pseudonymous, not anonymous.
            </p>
            <p className="privacy-note">
              <strong>Redacted text</strong> is the full text of this file after redaction, not a
              live transcript: scoring reads your resume separately and sends it alongside your
              preferences and the job posting, and detection can differ slightly between runs.
            </p>
            <pre className="privacy-preview">{highlight(privacy.redactedPreview)}</pre>
          </details>
        </>
      )}
    </section>
  );
}
