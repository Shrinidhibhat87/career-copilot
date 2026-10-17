from __future__ import annotations

from collections.abc import Sequence

import pytest

from career_copilot.adapters.pii.hybrid_detector import HybridDetector
from career_copilot.ports import Detection, PiiDetector, PiiRedactionError


class FakeDetector:
    def __init__(self, detections: Sequence[Sequence[Detection]] = ((),)) -> None:
        self._detections = detections

    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        return self._detections


class Broken:
    def detect(self, texts: Sequence[str]) -> Sequence[Sequence[Detection]]:
        raise RuntimeError("unavailable")


def test_it_satisfies_the_port_and_unions_without_duplicates() -> None:
    ada = Detection(0, 3, "PERSON")
    bob = Detection(8, 3, "PERSON")
    detector = HybridDetector(FakeDetector([[ada]]), FakeDetector([[ada, bob]]))
    assert isinstance(detector, PiiDetector)
    assert list(detector.detect(["Ada and Bob"])[0]) == [ada, bob]


def test_secondary_failure_blocks_by_default() -> None:
    with pytest.raises(PiiRedactionError, match="recall pass"):
        HybridDetector(FakeDetector(), Broken()).detect(["text"])


def test_secondary_failure_can_degrade_to_primary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ada = Detection(0, 3, "PERSON")
    detector = HybridDetector(FakeDetector([[ada]]), Broken(), "primary_only")
    with caplog.at_level("WARNING"):
        assert list(detector.detect(["Ada"])[0]) == [ada]
    assert detector.degraded is True
    assert "recall pass unavailable" in caplog.text


def test_primary_failure_always_blocks() -> None:
    with pytest.raises(PiiRedactionError, match="detection floor"):
        HybridDetector(Broken(), FakeDetector(), "primary_only").detect(["text"])


def test_wrong_cardinality_fails_closed() -> None:
    with pytest.raises(PiiRedactionError, match="result count"):
        HybridDetector(FakeDetector([[]]), FakeDetector([[], []])).detect(["text"])
