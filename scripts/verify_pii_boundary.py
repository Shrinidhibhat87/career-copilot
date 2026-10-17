"""Verify the provider boundary without printing or transmitting sensitive text.

Usage:
    uv run python scripts/verify_pii_boundary.py "<your name>" <your email> [more ...]
"""

from __future__ import annotations

import json
import sys
from typing import Any

from career_copilot.adapters.llm.litellm_client import LiteLLMClient
from career_copilot.adapters.llm.redacting_client import RedactingLLMClient
from career_copilot.adapters.pii.factory import build_redactor
from career_copilot.adapters.profile_loader import load_notes
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.agents.match import MatchAgent
from career_copilot.config import Config
from career_copilot.domain.profile import Profile


def main(probes: list[str]) -> int:
    if not probes:
        print('usage: verify_pii_boundary.py "<name>" <email> [more ...]')
        return 2

    config = Config.load()
    with SqliteStore(config.db_path) as cv_store:
        cv = cv_store.current_cv()
    if cv is None:
        print("FAIL: no CV stored. Upload one on the CV page first")
        return 2
    profile = Profile(preferences=load_notes(config.profile_dir), resume=cv.text)
    if any(not probe or probe not in profile.resume for probe in probes):
        print("FAIL: at least one probe does not occur in the resume; PASS would prove nothing")
        return 2

    redactor = build_redactor(config)
    if redactor is None:
        print("FAIL: PII redaction is disabled")
        return 1
    # Same order as the CLI: the owner identity is learned from the resume before any
    # prompt is built, so the anchor is active for the probe below.
    redactor.learn(profile.resume)

    leaked = False
    quoted_redacted = False

    def capture(**kwargs: Any) -> Any:
        """Stand in for the provider; inspect in memory and make no network call."""
        nonlocal leaked, quoted_redacted
        messages = kwargs["messages"]
        payload = json.dumps(messages)
        leaked = leaked or any(probe in payload for probe in probes)

        prompt = str(messages[-1]["content"])
        resume_section = prompt.partition("RESUME\n")[2].partition("\n\nPOSTING\n")[0]
        redacted_lines = [line for line in resume_section.splitlines() if "<|" in line]
        quoted_redacted = bool(redacted_lines)
        evidence = redacted_lines[0] if redacted_lines else "no placeholder found"
        reply = json.dumps(
            {
                "score": 50,
                "verdict": "possible",
                "reasons": ["boundary probe"],
                "gaps": [],
                "warnings": [],
                "evidence_refs": [evidence],
            }
        )
        message = type("Message", (), {"content": reply})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice], "_hidden_params": {}})()

    store = SqliteStore(config.db_path)
    try:
        jobs = store.list_jobs(limit=1)
    finally:
        store.close()
    if not jobs:
        print("FAIL: no stored jobs; run `career-copilot discover` first")
        return 2

    client = RedactingLLMClient(
        LiteLLMClient("probe", completion=capture),
        redactor,
        outbound=config.pii.outbound,
    )
    try:
        MatchAgent(client, profile).evaluate(jobs[0].posting)
    except Exception as exc:
        print(f"FAIL: verification did not complete ({type(exc).__name__})")
        return 1

    if leaked:
        print("FAIL: a probe value reached the provider payload")
        return 1
    if not quoted_redacted:
        print("FAIL: no placeholder appeared in resume evidence")
        return 1
    print("PASS: probes absent; redacted evidence restored and validated")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
