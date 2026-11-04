"""Reads Telegram button presses while an account is signed in (spec 020).

A thread beside the scheduler's ticker: started at sign-in, stopped at sign-out. It waits
quietly while the channel is off or not paired, and makes no request then.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from threading import Event, Thread

from career_copilot.adapters.secrets import default_secrets
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.ports import NotificationError
from career_copilot.usecases.notify import _active_bot, apply_replies

log = logging.getLogger(__name__)
IDLE_SECONDS = 30


class ReplyPoller:
    def __init__(self, config: Config, clock: Callable[[], datetime]) -> None:
        self._config = config
        self._clock = clock
        self._stop = Event()
        self._thread: Thread | None = None

    def begin(self) -> None:
        self._thread = Thread(target=self._run, name="telegram-replies", daemon=True)
        self._thread.start()

    def end(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            wait = IDLE_SECONDS
            try:
                with SqliteStore(self._config.db_path) as store:
                    bot = _active_bot(self._config, store, default_secrets())
                    if bot is not None:
                        apply_replies(store, bot, self._clock)
                        wait = 1
            except NotificationError as error:
                log.warning("telegram poll failed: %s", error.reason)
            except Exception as error:  # a poll must never take the server down
                log.warning("telegram poll error: %s", type(error).__name__)
            self._stop.wait(wait)
