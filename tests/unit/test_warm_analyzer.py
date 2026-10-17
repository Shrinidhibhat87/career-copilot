import logging
from types import SimpleNamespace

import pytest

from career_copilot.adapters.api.app import warm_analyzer
from career_copilot.adapters.pii.hybrid_detector import HybridDetector


class _Detector:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.warmed = 0

    def warm(self) -> None:
        self.warmed += 1
        if self.error is not None:
            raise self.error


def test_warms_the_detector() -> None:
    detector = _Detector()
    warm_analyzer(SimpleNamespace(detector=detector))
    assert detector.warmed == 1


@pytest.mark.parametrize("error", [RuntimeError("boom"), SystemExit(1)])
def test_a_failing_redactor_is_logged_not_raised(
    error: BaseException, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        warm_analyzer(SimpleNamespace(detector=_Detector(error)))
    assert "warm" in caplog.text.lower()


def test_no_redactor_or_no_warm_hook_is_a_no_op() -> None:
    warm_analyzer(None)
    warm_analyzer(SimpleNamespace(detector=object()))


def test_hybrid_detector_warms_its_primary() -> None:
    primary = _Detector()
    hybrid = HybridDetector(primary, _Detector())  # type: ignore[arg-type]
    hybrid.warm()
    assert primary.warmed == 1
