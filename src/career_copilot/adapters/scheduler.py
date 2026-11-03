"""The ticker behind scheduled runs (spec 019). Lives in the server process, for the
signed-in account only; `runs.start_session` starts it and `end_session` stops it.

It owns no run logic: `start` is `runs.request_refresh`, which already refuses billed
models, a missing CV and a down backend.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Any

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.schedule import due_slot, is_covered

log = logging.getLogger(__name__)

TICK_SECONDS = 60
RETRY_AFTER = timedelta(minutes=15)


class Scheduler:
    def __init__(
        self,
        config: Config,
        start: Callable[..., dict[str, Any]],
        busy: Callable[[], bool],
        clock: Callable[[], datetime],
    ) -> None:
        self._config, self._start, self._busy, self._clock = config, start, busy, clock
        self.last_skip: tuple[str, datetime] | None = None
        self._stop = Event()
        self._thread: Thread | None = None

    def tick(self) -> None:
        """Start one run if a slot is due and nothing covered it. Never raises."""
        try:
            self._tick()
        except Exception:
            log.exception("scheduler tick failed")

    def _tick(self) -> None:
        with SqliteStore(self._config.db_path) as store:
            schedule, last = store.schedule_setting(), store.last_run_started_at()
        now = self._clock()
        if schedule is None or not schedule.enabled:
            return
        slot = due_slot(schedule.times, now.astimezone())
        # A slot from before the schedule was saved is not owed: turning it on is not a missed run.
        if slot is None or slot < schedule.saved_at or is_covered(slot, last) or self._busy():
            return
        if self.last_skip and now - self.last_skip[1] < RETRY_AFTER:
            return
        result = self._start(self._config, trigger="scheduled", queue=False)
        self.last_skip = None if result.get("started") else (str(result.get("reason")), now)

    def begin(self) -> None:
        """The first tick runs at once, which is the catch-up after a sign-in."""
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        self.tick()
        while not self._stop.wait(TICK_SECONDS):
            self.tick()

    def end(self) -> None:
        self._stop.set()
