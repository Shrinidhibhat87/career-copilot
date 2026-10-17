"""Builds the `Redactor` the CLI wires into the LLM boundary (design doc §13).

Construction only. Detection itself is lazy: `PresidioDetector` builds its analyzer on the
first `detect()` call, so building a redactor here costs nothing on a `--dry-run` path that
never reaches a provider (§15, step 3).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from career_copilot.adapters.pii.hybrid_detector import HybridDetector
from career_copilot.adapters.pii.ollama_detector import OllamaDetector
from career_copilot.adapters.pii.person_policy import PersonPolicy
from career_copilot.adapters.pii.presidio_detector import PresidioDetector
from career_copilot.adapters.pii.redactor import Redactor
from career_copilot.config import Config
from career_copilot.domain.owner_identity import OwnerIdentity
from career_copilot.ports import PiiDetector

log = logging.getLogger(__name__)


def _string_list(raw: object, key: str, path: Path) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"identity override {path}: '{key}' must be a list of strings")
    return tuple(item.strip() for item in raw if item.strip())


def load_overrides(identity_path: str | None) -> OwnerIdentity | None:
    """Read the optional pinned-identity file.

    Absent is the expected state and says nothing. Present but malformed raises here, at
    build time, rather than being silently ignored — a file the user wrote to protect their
    own name failing quietly is the worst outcome of the three.
    """
    if not identity_path:
        return None
    path = Path(identity_path)
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"identity override {path}: expected a mapping")
    return OwnerIdentity(
        names=_string_list(raw.get("names"), "names", path),
        emails=_string_list(raw.get("emails"), "emails", path),
        phones=_string_list(raw.get("phones"), "phones", path),
        confidence="confirmed",
    )


def _build_policy(config: Config, skill_terms: Sequence[str] | None = None) -> PersonPolicy:
    """The exemption seed: the skill vocabulary in force, plus the built-in product terms,
    plus configured stragglers. Sourcing the first from the same list the hard filters use
    is what keeps the two from drifting — anything scored as a skill cannot be redacted as
    a person.

    `skill_terms` is the caller's current preferences where it has them; `config.yaml`
    `filters:` is the fallback for a caller that runs before any preferences are loaded.
    """
    source = config.filters.core_stack_terms if skill_terms is None else skill_terms
    terms = {term.lower() for term in source if term}
    terms |= {term.strip().lower() for term in config.pii.person_false_positive_terms if term}
    return PersonPolicy(
        false_positive_terms=frozenset(terms),
        drop_single_token_person=config.pii.drop_single_token_person,
    )


def build_redactor(config: Config, skill_terms: Sequence[str] | None = None) -> Redactor | None:
    """The always-on Presidio floor, or `None` when redaction is switched off.

    `enabled: false` is a loud choice (§13): it is named in a warning at run start rather
    than passing silently, because it sends raw resume text to the provider.
    """
    spec = config.pii
    if not spec.enabled:
        log.warning(
            "pii.enabled is false: prompts are sent to the model without redaction "
            "(set pii.enabled: true in config.yaml to restore the boundary)"
        )
        return None

    detector: PiiDetector = PresidioDetector(language=spec.language)
    if spec.llm_pass:
        host = urlsplit(spec.llm_endpoint).hostname
        loopback = host in {"localhost", "127.0.0.1", "::1"}
        if not loopback and not spec.llm_allow_remote:
            raise ValueError(
                "pii.llm_endpoint must be loopback unless pii.llm_allow_remote is true"
            )
        recall = OllamaDetector(
            spec.llm_endpoint,
            spec.llm_model,
            spec.llm_timeout_seconds,
        )
        recall.check_ready()
        detector = HybridDetector(
            detector,
            recall,
            on_secondary_failure=("block" if spec.llm_failure == "block" else "primary_only"),
        )
    return Redactor(
        detector=detector,
        entities=list(spec.entities),
        policy=_build_policy(config, skill_terms),
        owner_anchor=spec.owner_anchor,
        overrides=load_overrides(spec.identity_path),
    )
