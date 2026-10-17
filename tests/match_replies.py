"""Scripted match replies in the four-part shape, shared by every test that feeds a MatchAgent."""

from __future__ import annotations


def match_reply(
    skills: int = 32,
    seniority: int = 20,
    domain: int = 16,
    preferences: int = 12,
    **overrides: object,
) -> dict[str, object]:
    """Parts add up to 80 by default, the total the old fixtures used."""
    reply: dict[str, object] = {
        "skills": {"points": skills, "reason": "skills reason"},
        "seniority": {"points": seniority, "reason": "seniority reason"},
        "domain": {"points": domain, "reason": "domain reason"},
        "preferences": {"points": preferences, "reason": "preferences reason"},
        "reasons": ["agentic AI ownership"],
        "gaps": [],
        "warnings": [],
        "evidence_refs": ["L1"],
    }
    reply.update(overrides)
    return reply


def reply_with_total(total: int, **overrides: object) -> dict[str, object]:
    """A reply whose four parts add up to `total` (0..100), for tests that care about the
    total and the verdict band, not the split."""
    from career_copilot.domain.scoring import PART_MAX

    left = total
    points: dict[str, int] = {}
    for name, maximum in PART_MAX.items():
        points[name] = min(maximum, left)
        left -= points[name]
    return match_reply(
        skills=points["skills"],
        seniority=points["seniority"],
        domain=points["domain"],
        preferences=points["preferences"],
        **overrides,
    )
