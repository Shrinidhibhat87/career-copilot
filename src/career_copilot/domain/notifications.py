"""What a push carries and what a reply does (spec 020). Pure, no I/O."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from career_copilot.domain.scoring import ScoredJob

# Jobs per run, best first; the rest are counted, not sent.
CAP = 5

# The two buttons. Interested saves the job, Not interested dismisses it (recycle bin).
CHOICES: dict[str, str] = {"interested": "saved", "not_interested": "dismissed"}
LABELS: dict[str, str] = {"interested": "Interested", "not_interested": "Not interested"}
STATUS_MARK = "\n\n— "


@dataclass(frozen=True, slots=True)
class Notifications:
    """Which channels the person switched on. No contact detail lives here (see ports)."""

    version: int
    enabled: bool
    telegram: bool
    email: bool
    saved_at: datetime


@dataclass(frozen=True, slots=True)
class Reply:
    """A button press from the paired chat. The last three fields only let the adapter
    answer it."""

    update_id: int
    job_id: int
    choice: str
    message_id: int
    callback_id: str
    text: str


def pick(
    matches: Sequence[ScoredJob], sent: set[int], cap: int = CAP
) -> tuple[list[ScoredJob], int]:
    """The best `cap` matches not sent before, and how many more there were."""
    fresh = [m for m in matches if m.job.id not in sent]
    return fresh[:cap], max(0, len(fresh) - cap)


def mask_chat(chat_id: str) -> str:
    return f"chat …{chat_id[-4:]}"


def telegram_text(match: ScoredJob) -> str:
    """Plain text on purpose: a posting's words are never read as markup."""
    posting = match.job.posting
    lines = [
        f"{match.score.score} — {posting.title} · {posting.company}",
        posting.location or "location not stated",
        "",
        *[f"• {reason}" for reason in match.score.reasons],
        "",
        posting.canonical_url,
    ]
    return "\n".join(lines)


def decision_for(current: str | None, choice: str) -> str | None:
    """The decision a press appends. Pressing the choice already in force takes it back
    (`restored` counts as no decision); an applied job is left alone."""
    if current == "applied":
        return None
    target = CHOICES[choice]
    return "restored" if current == target else target


def shown_choice(decision: str | None, choice: str) -> str | None:
    """The choice the message should show after the press, or none once it is taken back."""
    return choice if decision == CHOICES[choice] else None


def mask_email(address: str) -> str:
    """`jane@gmail.com` -> `j***@gmail.com`."""
    name, _, domain = address.partition("@")
    return f"{name[:1]}***@{domain}"


def email_body(matches: Sequence[ScoredJob], more: int) -> str:
    """One plain-text digest: each match as in a Telegram message, then the count."""
    parts = [telegram_text(m) for m in matches]
    if more:
        parts.append(f"+{more} more in the app")
    return "\n\n---\n\n".join(parts)
