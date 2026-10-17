"""The condenser's prompt: every entry, in order, and nothing the model must invent."""

from __future__ import annotations

from career_copilot.agents.condenser import SYSTEM_PROMPT, condense_prompt


def test_prompt_numbers_entries_oldest_first() -> None:
    prompt = condense_prompt(["  FDE roles in Berlin ", "Actually remote is fine too"])
    first = prompt.index("1. FDE roles in Berlin")
    second = prompt.index("2. Actually remote is fine too")
    assert first < second
    assert "oldest first" in prompt


def test_system_prompt_keeps_requirements_and_placeholders() -> None:
    assert "dealbreakers" in SYSTEM_PROMPT
    assert "first person" in SYSTEM_PROMPT
    assert "<|PERSON_1|>" in SYSTEM_PROMPT
