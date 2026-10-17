from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime

import pytest

from career_copilot.domain.models import RawJob, SourceTarget

# `Config.load` picks a model profile by hardware. No test may depend on the machine it
# runs on, so profiles are off unless a test turns them back on. Set at import, not in a
# fixture: `adapters.api.app` loads its config while tests are still being collected.
os.environ["CAREER_COPILOT_PROFILE"] = "none"
# Nor on the machine's keychain: a key saved there must not leak into a test that expects
# none. `keyring` reads this when first asked for a backend.
os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 12, 9, 0, tzinfo=UTC)


def make_raw(**overrides: object) -> RawJob:
    """A permanent, Berlin-based, English-language role that passes every filter."""
    defaults: dict[str, object] = {
        "source": "greenhouse",
        "source_job_id": "1001",
        "company": "Acme GmbH",
        "title": "Forward Deployed Engineer (m/w/d)",
        "location": "Berlin, Germany",
        "url": "https://boards.greenhouse.io/acme/jobs/1001?utm_source=linkedin",
        "description": "Permanent full-time role building agentic AI systems with customers.",
    }
    defaults.update(overrides)
    return RawJob(**defaults)  # type: ignore[arg-type]


class FakeSource:
    """In-memory JobSource. Can be told to fail, to prove a run survives it."""

    def __init__(self, name: str, jobs: list[RawJob], fail: bool = False) -> None:
        self.name = name
        self.jobs = jobs
        self.fail = fail
        self.calls = 0

    def fetch(self, target: SourceTarget) -> Iterable[RawJob]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("board unreachable")
        return list(self.jobs)


@pytest.fixture(autouse=True)
def _signed_in(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """The API suite tests routes, not the gate: it runs as an already signed-in person on
    whatever `db_path` the test set. Tests of the gate itself carry `real_auth`."""
    import sys

    if (
        request.node.get_closest_marker("real_auth")
        or "career_copilot.adapters.api.app" not in sys.modules
    ):
        return
    from career_copilot.adapters.api import app as api
    from career_copilot.adapters.api import auth

    monkeypatch.setattr(auth, "authenticated", lambda request: True)
    monkeypatch.setattr(api, "_user_config", lambda: api._config)
