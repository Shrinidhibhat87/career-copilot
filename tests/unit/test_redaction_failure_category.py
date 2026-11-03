from __future__ import annotations

from career_copilot.ports import FAILURE_MESSAGES, FailureCategory, PiiModelMissingError


def test_a_redaction_failure_is_not_reported_as_a_transport_failure() -> None:
    error = PiiModelMissingError("en_core_web_lg")
    assert error.category is FailureCategory.REDACTION
    assert "nothing was sent" in FAILURE_MESSAGES[error.category]
