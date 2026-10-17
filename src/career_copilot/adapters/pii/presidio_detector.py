"""Presidio — the always-on local detection floor (design doc §7.1).

Rule-based recognizers (email, IBAN, credit card, IP, URL, phone) are exact. spaCy NER adds
`PERSON`, which is statistical and not complete recall — that gap is what the optional Ollama
pass (a later task) exists to narrow. Importing this module must never load spaCy: the
analyzer is built lazily, on first `detect()` call, because both the FastAPI app and the CLI
import it transitively and neither should pay the model-load cost just to start.
"""

from __future__ import annotations

from collections.abc import Sequence

from career_copilot.ports import (
    Detection,
    PiiDetector,
    PiiModelMissingError,
    PiiRedactionError,
)

# Presidio entity -> project category. Anything not listed here is dropped, never guessed at
# (design doc §7.1).
#
# `LOCATION` is deliberately absent. Presidio's `LOCATION` fires on any geographic entity,
# cities included, and city is a first-class scoring signal in this project:
# `domain/filters.py` gates on `allowed_cities`/`preferred_cities`, and the match prompt
# carries `posting.location`. Mapping `LOCATION` to `ADDRESS` would redact "Berlin" out of
# both the resume and every job posting and degrade exactly what the product is for. `ADDRESS`
# here means *street address only*, sourced from the pattern recognizer registered below.
_ENTITY_MAP: dict[str, str] = {
    "PERSON": "PERSON",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "URL": "URL",
    "IBAN_CODE": "IBAN",
    "CREDIT_CARD": "CREDIT_CARD",
    "IP_ADDRESS": "IP",
    # The street-address pattern recognizer below is registered with supported_entity
    # "ADDRESS" directly, so its findings already carry the project's own category name.
    "ADDRESS": "ADDRESS",
}

# Street address only: street name + house number, with an optional postcode + city.
# Two orderings, matching the user's own resume and German job postings (§7.1's brief):
#   - German: "Musterstraße 12, 10115 Berlin" (street+number first, suffix on the street name)
#   - English: "12 Baker Street" (number first, suffix word after the street name)
# Kept conservative on purpose — a false positive here eats real scoring text (résumé prose,
# job description body), which is worse than an occasional missed address; the Ollama recall
# pass and a human glance at the redaction preview are the backstops for what this misses.
#
# `-ring` is a common, real German street suffix ("Sachsenring", "Kurfürstenring",
# "Adenauerring"), and as a *bare* suffix it also matches the tail of any capitalised word
# ending in "-ring" in ordinary prose — "Spring 2026", "Hiring 5", "Monitoring 24". A full
# ban on the suffix (fix round 1) closed the false positives but opened a real recall gap for
# exactly the address form a Berlin-based user is likely to have. The fix here is a separate
# pattern for `-ring` streets that requires the trailing ", <postcode> <city>" context to be
# *mandatory*, not optional as it is for the other suffixes: "Sachsenring 12, 50733 Köln"
# matches, but "Spring 2026" and "Hiring 5" have no comma-postcode-city tail to satisfy it, so
# they still cannot match. This does mean a bare "Sachsenring 12" with no postcode is not
# caught — see the report's residual-gap note.
#
# `Chaussee` and `Damm` are common German street suffixes with no gerund-collision risk (no
# common English word ends in either), so they join the safe, optional-postcode-context list
# rather than needing the mandatory-context treatment `ring` gets.
#
# The English list is restricted to full suffix words (`Street`, not `St`; `Court`, not
# `Ct`; no bare `Way`). The two/three-letter abbreviations collided with ordinary capitalised
# words in prose ("5 Star Ct", "3 Step Way", "12 Baker Dr[ive as a name]"): an abbreviation
# carries too little signal on its own to justify that cost, and this recognizer does not
# special-case a trailing period to recover them — see the report's residual-gap note.
_STREET_ADDRESS_PATTERNS = [
    (
        "street_address_de",
        r"\b[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.-]*(?:straße|strasse|weg|allee|platz|gasse|chaussee|damm)"
        r"\s+\d{1,4}[a-zA-Z]?(?:,\s*\d{4,5}\s+[A-ZÄÖÜ][A-Za-zäöüßÄÖÜ]+)?\b",
    ),
    (
        "street_address_de_ring",
        # Same shape as street_address_de, but the postcode + city tail is required rather
        # than optional — that's what keeps "Sachsenring 12, 50733 Köln" matching while
        # denying "Spring 2026" / "Hiring 5" / "Monitoring 24", none of which has one.
        r"\b[A-ZÄÖÜ][A-Za-zÄÖÜäöüß.-]*ring"
        r"\s+\d{1,4}[a-zA-Z]?,\s*\d{4,5}\s+[A-ZÄÖÜ][A-Za-zäöüßÄÖÜ]+\b",
    ),
    (
        "street_address_en",
        r"\b\d{1,5}[a-zA-Z]?\s+[A-Z][A-Za-z]+(?:\s[A-Z][A-Za-z]+)?"
        r"\s(?:Street|Road|Avenue|Lane|Drive|Boulevard|Court)\b",
    ),
]


def _build_analyzer(model_name: str, language: str) -> object:
    """Constructs a Presidio `AnalyzerEngine` pinned to one spaCy model, with the street
    address recognizer registered. Imports spaCy-dependent Presidio modules only here, so
    module import stays cheap.

    Returns `object` rather than `AnalyzerEngine` in the signature: presidio-analyzer ships
    `py.typed` but its own constructor and recognizer signatures are loosely typed (many
    `Optional[...]`/implicit `Any` parameters), which would otherwise leak untyped values
    into this module's strictly-typed surface. The concrete type is used, just not named,
    keeping the `# type: ignore` scoped to the two calls below instead of this function.
    """
    import spacy
    from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    # Presidio's own loader falls back to `spacy.cli.download(...)` for a model it can't
    # find, which would silently reach out to the network mid-run for what should be a local,
    # offline detection floor. Check the model is already installed and fail otherwise —
    # this is a local-first tool with no scanning-time network dependency (§7.1).
    if not spacy.util.is_package(model_name):
        raise PiiModelMissingError(model_name)

    nlp_configuration = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": language, "model_name": model_name}],
    }
    nlp_engine = NlpEngineProvider(nlp_configuration=nlp_configuration).create_engine()
    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=[language])

    for name, regex in _STREET_ADDRESS_PATTERNS:
        recognizer = PatternRecognizer(
            supported_entity="ADDRESS",
            name=name,
            supported_language=language,
            patterns=[Pattern(name=name, regex=regex, score=0.85)],
        )
        analyzer.registry.add_recognizer(recognizer)

    return analyzer


class PresidioDetector:
    """`ports.PiiDetector` over Presidio: the always-on local detection floor.

    The spaCy model name and language are constructor arguments, not hardcoded, so tests can
    use the small model and the §17 model-size question (`en_core_web_sm` vs `_lg`) is
    configuration, not a code change.
    """

    def __init__(self, model_name: str = "en_core_web_lg", language: str = "en") -> None:
        self._model_name = model_name
        self._language = language
        self._analyzer: object | None = None

    def _get_analyzer(self) -> object:
        if self._analyzer is None:
            try:
                self._analyzer = _build_analyzer(self._model_name, self._language)
            except PiiModelMissingError:
                # Already the precise failure, and it names only a config constant. Wrapping
                # it would throw away the one remedy a caller can act on.
                raise
            except (Exception, SystemExit) as exc:
                # SystemExit too: a spaCy CLI failure path (e.g. a still-attempted model
                # download) calls sys.exit rather than raising, and that must fail closed
                # exactly like any other construction failure, not tear down the process.
                # Never name the analysed text or config values that could echo user data —
                # only the failure class (design doc §11).
                raise PiiRedactionError(
                    f"Presidio analyzer could not be constructed ({type(exc).__name__})"
                ) from exc
        return self._analyzer

    def warm(self) -> None:
        """Build the analyzer now, so the first `detect` call is not the slow one."""
        self._get_analyzer()

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        analyzer = self._get_analyzer()
        results: list[Sequence[Detection]] = []
        for text in texts:
            try:
                # mypy: `analyzer` is `object` per _build_analyzer's return; presidio's own
                # `.analyze` signature is loosely typed regardless. See _build_analyzer docstring.
                findings = analyzer.analyze(text=text, language=self._language)  # type: ignore[attr-defined]
            except Exception as exc:
                raise PiiRedactionError(f"Presidio analysis failed ({type(exc).__name__})") from exc
            detections = [
                Detection(
                    start=finding.start,
                    length=finding.end - finding.start,
                    category=_ENTITY_MAP[finding.entity_type],
                )
                for finding in findings
                if finding.entity_type in _ENTITY_MAP
            ]
            results.append(detections)
        return results


# Static type-check that the runtime class satisfies the port, without importing spaCy at
# module load time (the Protocol check itself costs nothing).
_: type[PiiDetector] = PresidioDetector
