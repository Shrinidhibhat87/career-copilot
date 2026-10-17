"""The outline agent copies sentences out of a posting and refuses anything it did not say."""

from __future__ import annotations

import pytest

from career_copilot.agents.outline import (
    MAX_BULLET_CHARS,
    MAX_BULLETS,
    OutlineAgent,
    OutlineUnavailable,
    ground,
)
from career_copilot.domain.normalize import to_posting
from career_copilot.ports import LLMResponse
from tests.conftest import make_raw

POSTING = (
    "KINEXON Sports stands as a global pioneer in tracking technology. "
    "YOUR TASKS Design and build backend services for our positioning system. "
    "Review code and mentor junior engineers. "
    "YOUR QUALIFICATIONS Solid experience with Go, Python or C++. "
    "A degree in computer science or a related field."
)
# How some boards arrive after the tags are stripped: sections glued to the sentence before.
GLUED = "The RoleYour responsibilities will include:Implement and improve CI/CD processes."


def test_a_sentence_in_the_posting_is_kept() -> None:
    assert ground(["Review code and mentor junior engineers."], POSTING) == (
        "Review code and mentor junior engineers.",
    )


def test_a_reworded_or_invented_sentence_is_dropped() -> None:
    reworded = ["You will mentor junior engineers by reviewing code.", "Five years of Rust."]
    assert ground(reworded, POSTING) == ()


def test_line_breaks_and_glued_words_do_not_hide_a_verbatim_quote() -> None:
    assert ground(["Implement and improve\nCI/CD processes."], GLUED) == (
        "Implement and improve CI/CD processes.",
    )


def test_headings_and_repeats_are_dropped() -> None:
    sentence = "Solid experience with Go, Python or C++."
    assert ground(["YOUR TASKS", sentence, sentence], POSTING) == (sentence,)


def test_a_list_is_capped_and_a_long_sentence_trimmed() -> None:
    long_sentence = "Own " + "the whole delivery pipeline end to end " * 12 + "."
    text = " ".join(f"Responsibility number {n} is yours." for n in range(10)) + " " + long_sentence
    bullets = [f"Responsibility number {n} is yours." for n in range(10)]
    assert len(ground(bullets, text)) == MAX_BULLETS
    trimmed = ground([long_sentence], text)[0]
    assert len(trimmed) <= MAX_BULLET_CHARS + 1
    assert trimmed.endswith("…")


def test_something_that_is_not_a_list_is_ignored() -> None:
    assert ground("Review code and mentor junior engineers.", POSTING) == ()
    assert ground(None, POSTING) == ()


class ScriptedLLM:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data

    def complete(
        self, prompt: str, schema: object, system: str | None = None, validator: object = None
    ) -> LLMResponse:
        return LLMResponse(data=self.data, model="test-model")


def test_the_agent_returns_only_grounded_sentences() -> None:
    posting = to_posting(make_raw(description=POSTING))
    llm = ScriptedLLM(
        {
            "responsibilities": ["Review code and mentor junior engineers.", "Invented duty here."],
            "requirements": ["A degree in computer science or a related field."],
        }
    )
    result = OutlineAgent(llm).outline(posting)
    assert result.responsibilities == ("Review code and mentor junior engineers.",)
    assert result.requirements == ("A degree in computer science or a related field.",)
    assert result.model == "test-model"


def test_nothing_grounded_is_a_failure_not_an_empty_outline() -> None:
    posting = to_posting(make_raw(description=POSTING))
    llm = ScriptedLLM({"responsibilities": ["Made up entirely, sorry."], "requirements": []})
    with pytest.raises(OutlineUnavailable):
        OutlineAgent(llm).outline(posting)
