"""Deep scoring. Every claimed match must cite a resume line."""

from __future__ import annotations

import re
from dataclasses import dataclass

from career_copilot.agents import as_tuple, prompt_hash
from career_copilot.domain.ad_text import clean_ad
from career_copilot.domain.models import JobPosting
from career_copilot.domain.profile import Profile
from career_copilot.domain.scoring import (
    PART_MAX,
    SENIORITY_GATE_SKILLS_MIN,
    SENIORITY_GATED_SHARE,
    ScoreVerdict,
)
from career_copilot.ports import FailureCategory, LLMClient, SchemaError


def _part_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"points": {"type": "integer"}, "reason": {"type": "string"}},
        "required": ["points", "reason"],
    }


# The model returns the four parts; code adds them. There is no `score` or `verdict` field to
# disagree with the parts.
SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        **{name: _part_schema() for name in PART_MAX},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "warnings": {"type": "array", "items": {"type": "string"}},
        # Resume line IDs such as "L12", never quoted text.
        "evidence_refs": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
    "required": [*PART_MAX, "reasons", "gaps", "warnings", "evidence_refs"],
}

SYSTEM = (
    "You score a job posting against a candidate's resume and preferences. "
    "Every resume line is prefixed with an ID like [L12]. Every entry in reasons must be "
    'supported by an entry in evidence_refs citing that line\'s ID, for example "L12". '
    "Cite IDs only; never quote or paraphrase the resume. Never infer a skill, credential, "
    "language level, or eligibility that the resume does not state. Name real gaps in gaps. "
    "If you cannot cite the resume, award few points. "
    "Score four parts, each an integer from 0 up to its maximum, with one short plain sentence "
    "of reason: skills (max 40, how well the resume's skills cover the posting's requirements), "
    "seniority (max 25, how well the scope and ownership of the role fit the candidate; "
    "a stated number of years is a weak signal, so never award or deny seniority points "
    "for years alone, and count years only where the resume also covers the posting's "
    "required skills), "
    "domain (max 20, how close the industry and problem area are), "
    "preferences (max 15, how well location, language and the stated preferences fit). "
    "Do not return a total."
)


# Added to the system prompt only when a LEARNED section is present.
LEARNED_RULE = (
    " A LEARNED section lists roles the candidate saved or dismissed. Use it only as a hint "
    "for the preferences part: lower it for roles close to what they dismissed, raise it for "
    "roles close to what they saved. It is never evidence for skills, and never a reason "
    "to cite anything but the resume."
)

_LINE_ID = re.compile(r"\[?\s*L?(\d+)\s*\]?", re.IGNORECASE)


def _numbered_lines(resume: str) -> dict[int, str]:
    """Non-blank resume lines keyed by their real 1-based line number, so IDs are stable."""
    return {n: line.strip() for n, line in enumerate(resume.splitlines(), 1) if line.strip()}


def _line_id(ref: str) -> int | None:
    match = _LINE_ID.fullmatch(ref.strip())
    return int(match.group(1)) if match else None


def _read_parts(data: dict[str, object]) -> dict[str, PartScore]:
    """The four parts, each inside its range. A part out of range is a schema failure, which
    makes the client feed the error back for its one retry."""
    parts: dict[str, PartScore] = {}
    for name, maximum in PART_MAX.items():
        raw = data.get(name)
        points = raw.get("points") if isinstance(raw, dict) else None
        reason = raw.get("reason") if isinstance(raw, dict) else None
        if not isinstance(points, int) or isinstance(points, bool):
            raise SchemaError(f"{name}.points must be an integer")
        if not isinstance(reason, str):
            raise SchemaError(f"{name}.reason must be a sentence")
        if not 0 <= points <= maximum:
            raise SchemaError(f"{name}.points must be between 0 and {maximum}, got {points}")
        parts[name] = PartScore(points=points, reason=reason.strip())
    return parts


def gate_seniority(parts: dict[str, PartScore]) -> dict[str, PartScore]:
    """Cap seniority when skills coverage is low, so a level or years match never carries a
    role whose required stack the resume does not cover."""
    skills_floor = PART_MAX["skills"] * SENIORITY_GATE_SKILLS_MIN
    cap = int(PART_MAX["seniority"] * SENIORITY_GATED_SHARE)
    seniority = parts["seniority"]
    if parts["skills"].points >= skills_floor or seniority.points <= cap:
        return parts
    reason = f"{seniority.reason} (capped: the resume covers too little of the required skills)"
    return {**parts, "seniority": PartScore(points=cap, reason=reason)}


class MissingEvidenceError(SchemaError):
    """A match claim with no resume citation. Never recorded as a score."""

    def __init__(self, message: str) -> None:
        super().__init__(message, category=FailureCategory.EVIDENCE)


@dataclass(frozen=True, slots=True)
class PartScore:
    """What one part of the score earned, and the sentence that says why."""

    points: int
    reason: str


@dataclass(frozen=True, slots=True)
class MatchResult:
    score: int
    verdict: ScoreVerdict
    reasons: tuple[str, ...]
    gaps: tuple[str, ...]
    warnings: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    parts: dict[str, PartScore]
    prompt_hash: str
    model: str
    cost_eur: float
    calls: int = 1


class MatchAgent:
    """One call per job that survived triage into the budget."""

    SCHEMA_PROPERTIES = frozenset(SCHEMA["properties"])  # type: ignore[call-overload]

    def __init__(self, client: LLMClient, profile: Profile, learned: str = "") -> None:
        self._client = client
        self._profile = profile
        # What the user's saves and dismissals say about their taste. Empty for a new
        # account, in which case the prompt is exactly what it was before.
        self._learned = learned.strip()

    @property
    def model(self) -> str:
        """The model name the store stamps scores with."""
        return str(getattr(self._client, "model", "unknown"))

    def build_prompt(self, posting: JobPosting) -> str:
        learned = f"LEARNED FROM THE CANDIDATE'S CHOICES\n{self._learned}\n\n"
        return (
            f"PREFERENCES\n{self._profile.preferences}\n\n"
            f"{learned if self._learned else ''}"
            f"RESUME\n{self._numbered_resume()}\n\n"
            f"POSTING\n"
            f"Title: {posting.title}\n"
            f"Company: {posting.company}\n"
            f"Location: {posting.location or 'not stated'}\n"
            f"Employment type: {posting.employment_type or 'not stated'}\n"
            f"URL: {posting.canonical_url}\n\n"
            f"{clean_ad(posting.description)}\n"
        )

    def evaluate(self, posting: JobPosting) -> MatchResult:
        prompt = self.build_prompt(posting)
        response = self._client.complete(
            prompt,
            SCHEMA,
            system=SYSTEM + LEARNED_RULE if self._learned else SYSTEM,
            validator=self._validate_evidence,
        )
        evidence = self._resolve_evidence(as_tuple(response.data.get("evidence_refs")))
        if not evidence:
            raise MissingEvidenceError(f"no resume evidence cited for {posting.canonical_url}")
        parts = gate_seniority(_read_parts(response.data))
        score = sum(part.points for part in parts.values())
        return MatchResult(
            score=score,
            # Derived, never trusted from the model, so band and number always agree.
            verdict=ScoreVerdict.from_score(score),
            reasons=as_tuple(response.data.get("reasons")),
            gaps=as_tuple(response.data.get("gaps")),
            warnings=as_tuple(response.data.get("warnings")),
            evidence_refs=evidence,
            parts=parts,
            prompt_hash=prompt_hash(prompt),
            model=response.model,
            cost_eur=response.cost_eur,
            calls=response.calls,
        )

    def _numbered_resume(self) -> str:
        return "\n".join(
            f"[L{n}] {line}" for n, line in _numbered_lines(self._profile.resume).items()
        )

    def _resolve_evidence(self, refs: tuple[str, ...]) -> tuple[str, ...]:
        """Resume text for each cited ID, in order, without repeats. Unknown IDs drop out."""
        lines = _numbered_lines(self._profile.resume)
        ids = dict.fromkeys(_line_id(ref) for ref in refs)
        return tuple(lines[n] for n in ids if n in lines)

    def _validate_evidence(self, data: dict[str, object]) -> None:
        _read_parts(data)
        evidence = as_tuple(data.get("evidence_refs"))
        if not evidence:
            raise MissingEvidenceError("evidence_refs must cite at least one resume line ID")
        lines = _numbered_lines(self._profile.resume)
        unknown = [ref for ref in evidence if _line_id(ref) not in lines]
        if unknown:
            raise MissingEvidenceError(
                "every evidence_refs entry must be a resume line ID such as L12"
            )
