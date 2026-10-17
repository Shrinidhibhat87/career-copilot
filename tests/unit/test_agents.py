from __future__ import annotations

import json

import pytest

from career_copilot.adapters.llm.fake import FakeLLM
from career_copilot.agents.match import MatchAgent
from career_copilot.agents.triage import TriageAgent
from career_copilot.domain.normalize import to_posting
from career_copilot.domain.profile import Profile
from career_copilot.domain.scoring import PART_MAX, ScoreVerdict
from career_copilot.ports import FailureCategory, SchemaError
from tests.conftest import make_raw
from tests.match_replies import match_reply, reply_with_total

PROFILE = Profile(preferences="Forward-deployed AI roles in Berlin.", resume="Built LangGraph POC")


def test_triage_returns_relevance_and_reason() -> None:
    client = FakeLLM(default={"relevance": 77, "reason": "agentic AI ownership"})
    result = TriageAgent(client, PROFILE.preferences).evaluate(to_posting(make_raw()))
    assert result.relevance == 77
    assert result.reason == "agentic AI ownership"
    assert result.model == "fake"


def test_triage_prompt_carries_the_posting_and_preferences() -> None:
    client = FakeLLM(default={"relevance": 1, "reason": "x"})
    TriageAgent(client, PROFILE.preferences).evaluate(to_posting(make_raw()))
    prompt = client.calls[0]
    assert "Forward Deployed Engineer" in prompt
    assert "Acme GmbH" in prompt
    assert "Forward-deployed AI roles in Berlin." in prompt


def test_triage_truncates_the_description() -> None:
    client = FakeLLM(default={"relevance": 1, "reason": "x"})
    long_description = "word " * 2000
    agent = TriageAgent(client, PROFILE.preferences, description_chars=100)
    agent.evaluate(to_posting(make_raw(description=long_description)))
    assert len(client.calls[0]) < 2000


def test_triage_rejects_an_out_of_range_relevance() -> None:
    client = FakeLLM(default={"relevance": 250, "reason": "x"})
    with pytest.raises(SchemaError):
        TriageAgent(client, PROFILE.preferences).evaluate(to_posting(make_raw()))


def test_match_returns_a_full_result() -> None:
    client = FakeLLM(
        default=reply_with_total(
            82,
            reasons=["agentic AI production ownership"],
            gaps=["no Kubernetes in the resume"],
            evidence_refs=["L1"],
        )
    )
    result = MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert result.score == 82
    assert result.verdict is ScoreVerdict.STRONG
    assert result.reasons == ("agentic AI production ownership",)
    assert result.gaps == ("no Kubernetes in the resume",)
    assert result.evidence_refs == ("Built LangGraph POC",)


def test_match_prompt_carries_resume_and_preferences() -> None:
    client = FakeLLM(default=reply_with_total(1, reasons=[], gaps=[], evidence_refs=["L1"]))
    MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert "[L1] Built LangGraph POC" in client.calls[0]
    assert "Forward-deployed AI roles in Berlin." in client.calls[0]


def test_empty_evidence_refs_is_rejected() -> None:
    client = FakeLLM(default=reply_with_total(95, reasons=["great fit"], gaps=[], evidence_refs=[]))
    with pytest.raises(SchemaError) as caught:
        MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert caught.value.category is FailureCategory.SCHEMA


def test_fabricated_evidence_is_rejected() -> None:
    client = FakeLLM(
        default=reply_with_total(
            95, reasons=["invented experience"], gaps=[], evidence_refs=["L99"]
        )
    )
    with pytest.raises(SchemaError, match="line ID") as caught:
        MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert caught.value.category is FailureCategory.EVIDENCE


LATEX_PROFILE = Profile(
    preferences="prefs",
    resume="\\section{Experience}\n\n\\item \\textbf{Built} LangGraph POC; led 3 engineers.\n",
)


def _match_reply(*refs: str) -> dict[str, object]:
    return reply_with_total(80, reasons=["x"], evidence_refs=list(refs))


def test_line_ids_number_the_real_file_lines_and_skip_blank_ones() -> None:
    client = FakeLLM(default=_match_reply("L3"))
    MatchAgent(client, LATEX_PROFILE).evaluate(to_posting(make_raw()))
    assert "[L1] \\section{Experience}" in client.calls[0]
    assert "[L3] \\item" in client.calls[0]
    assert "[L2]" not in client.calls[0]


def test_a_cited_id_resolves_to_the_raw_latex_line_despite_markup_and_punctuation() -> None:
    client = FakeLLM(default=_match_reply("L3"))
    result = MatchAgent(client, LATEX_PROFILE).evaluate(to_posting(make_raw()))
    assert result.evidence_refs == ("\\item \\textbf{Built} LangGraph POC; led 3 engineers.",)


@pytest.mark.parametrize("ref", ["L3", "[L3]", " l3 ", "3"])
def test_line_id_spellings_are_tolerated(ref: str) -> None:
    client = FakeLLM(default=_match_reply(ref, "L3"))
    result = MatchAgent(client, LATEX_PROFILE).evaluate(to_posting(make_raw()))
    assert len(result.evidence_refs) == 1


def test_a_blank_line_id_is_rejected() -> None:
    client = FakeLLM(default=_match_reply("L2"))
    with pytest.raises(SchemaError, match="line ID") as caught:
        MatchAgent(client, LATEX_PROFILE).evaluate(to_posting(make_raw()))
    assert caught.value.category is FailureCategory.EVIDENCE


def test_a_quoted_sentence_instead_of_an_id_is_rejected() -> None:
    client = FakeLLM(default=_match_reply("Built LangGraph POC"))
    with pytest.raises(SchemaError, match="line ID"):
        MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))


def test_verdict_is_derived_from_the_score_not_the_model() -> None:
    client = FakeLLM(default=reply_with_total(20, reasons=[], gaps=[], evidence_refs=["L1"]))
    result = MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert result.verdict is ScoreVerdict.WEAK


def test_the_same_posting_produces_the_same_prompt_hash() -> None:
    client = FakeLLM(default={"relevance": 1, "reason": "x"})
    agent = TriageAgent(client, PROFILE.preferences)
    posting = to_posting(make_raw())
    assert agent.evaluate(posting).prompt_hash == agent.evaluate(posting).prompt_hash


def test_the_total_is_the_sum_of_the_four_parts() -> None:
    client = FakeLLM(default=match_reply(skills=30, seniority=20, domain=15, preferences=10))
    result = MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert result.score == 75
    assert result.verdict is ScoreVerdict.STRONG
    assert {name: part.points for name, part in result.parts.items()} == {
        "skills": 30,
        "seniority": 20,
        "domain": 15,
        "preferences": 10,
    }
    assert result.parts["skills"].reason == "skills reason"


def test_the_same_parts_always_give_the_same_total() -> None:
    client = FakeLLM(default=match_reply(skills=30, seniority=20, domain=15, preferences=10))
    totals = {MatchAgent(client, PROFILE).evaluate(to_posting(make_raw())).score for _ in range(3)}
    assert totals == {75}


def test_the_model_is_not_asked_for_a_total() -> None:
    assert "score" not in MatchAgent.SCHEMA_PROPERTIES
    assert set(PART_MAX) <= set(MatchAgent.SCHEMA_PROPERTIES)


def test_a_part_above_its_maximum_is_a_schema_failure() -> None:
    client = FakeLLM(default=match_reply(skills=41))
    with pytest.raises(SchemaError, match="skills") as caught:
        MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert caught.value.category is FailureCategory.SCHEMA


def test_a_negative_part_is_a_schema_failure() -> None:
    client = FakeLLM(default=match_reply(domain=-1))
    with pytest.raises(SchemaError, match="domain"):
        MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))


def test_a_part_without_a_reason_is_a_schema_failure() -> None:
    reply = match_reply()
    reply["seniority"] = {"points": 10}
    with pytest.raises(SchemaError, match="seniority"):
        MatchAgent(FakeLLM(default=reply), PROFILE).evaluate(to_posting(make_raw()))


def test_an_out_of_range_part_runs_the_existing_retry_once() -> None:
    """The validator feeds the error back through LiteLLMClient's one retry."""
    from career_copilot.adapters.llm.litellm_client import LiteLLMClient
    from tests.contract.test_litellm_client import stub_completion

    bad = json.dumps(match_reply(skills=41))
    good = json.dumps(match_reply(skills=40))
    completion = stub_completion([bad, good])
    result = MatchAgent(LiteLLMClient(model="m", completion=completion), PROFILE).evaluate(
        to_posting(make_raw())
    )
    assert len(completion.calls) == 2
    assert result.parts["skills"].points == 40


def test_seniority_is_capped_when_skills_coverage_is_low() -> None:
    client = FakeLLM(default=match_reply(skills=10, seniority=25, domain=10, preferences=10))
    result = MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert result.parts["seniority"].points == 10
    assert "capped" in result.parts["seniority"].reason
    assert result.score == 10 + 10 + 10 + 10


def test_seniority_is_kept_when_skills_cover_the_posting() -> None:
    client = FakeLLM(default=match_reply(skills=30, seniority=25, domain=10, preferences=10))
    result = MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert result.parts["seniority"].points == 25
    assert "capped" not in result.parts["seniority"].reason


def test_a_low_seniority_is_never_raised_by_the_gate() -> None:
    client = FakeLLM(default=match_reply(skills=5, seniority=4, domain=5, preferences=5))
    result = MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    assert result.parts["seniority"].points == 4


def test_the_prompt_treats_years_as_a_weak_signal() -> None:
    client = FakeLLM(default=match_reply())
    MatchAgent(client, PROFILE).evaluate(to_posting(make_raw()))
    from career_copilot.agents.match import SYSTEM

    assert "years" in SYSTEM and "weak signal" in SYSTEM


def test_match_prompt_has_no_learned_block_by_default() -> None:
    agent = MatchAgent(FakeLLM(), Profile(preferences="Berlin", resume="Python"))
    prompt = agent.build_prompt(to_posting(make_raw(), {}))
    assert "LEARNED" not in prompt


def test_match_prompt_carries_the_learned_note_before_the_resume() -> None:
    agent = MatchAgent(
        FakeLLM(),
        Profile(preferences="Berlin", resume="Python"),
        learned="Roles the candidate dismissed:\n- Sales Manager at Acme",
    )
    prompt = agent.build_prompt(to_posting(make_raw(), {}))
    assert "LEARNED FROM THE CANDIDATE'S CHOICES\nRoles the candidate dismissed" in prompt
    assert prompt.index("LEARNED") < prompt.index("RESUME")
