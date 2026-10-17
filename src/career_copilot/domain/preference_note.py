"""What the user's saves and dismissals say about their taste, as a short note.

Pure: decisions in, text out. The match step reads the note next to the CV, so a job like
one the user already dismissed is judged with that in view. It is a hint about fit
(location, role, seniority), never evidence about skills.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

REASON_LABELS: dict[str, str] = {
    "wrong_role": "wrong role",
    "seniority": "wrong seniority",
    "location": "wrong location",
    "company": "company they do not want",
    "other": "other reasons",
}

# Per side. A handful of titles carries the pattern; more only costs tokens on every call.
EXAMPLES_PER_SIDE = 8


@dataclass(frozen=True, slots=True)
class DecisionSignal:
    decision: str  # saved | dismissed
    title: str
    company: str
    reason: str | None = None


def build_preference_note(signals: Sequence[DecisionSignal]) -> str:
    """The note, newest decisions first. Empty when there is nothing to say."""
    saved = [s for s in signals if s.decision == "saved"]
    dismissed = [s for s in signals if s.decision == "dismissed"]
    if not saved and not dismissed:
        return ""
    lines: list[str] = []
    if saved:
        lines.append("Roles the candidate saved (they want more like these):")
        lines.extend(f"- {_role(s)}" for s in saved[:EXAMPLES_PER_SIDE])
    if dismissed:
        lines.append("Roles the candidate dismissed (they want fewer like these):")
        lines.extend(f"- {_role(s)}" for s in dismissed[:EXAMPLES_PER_SIDE])
        reasons = Counter(s.reason for s in dismissed if s.reason in REASON_LABELS)
        if reasons:
            counted = ", ".join(
                f"{REASON_LABELS[reason]} ({count})" for reason, count in reasons.most_common()
            )
            lines.append(f"Why they dismissed: {counted}.")
    return "\n".join(lines)


def _role(signal: DecisionSignal) -> str:
    text = f"{signal.title} at {signal.company}"
    if signal.reason in REASON_LABELS:
        text += f" [{REASON_LABELS[signal.reason]}]"
    return text
