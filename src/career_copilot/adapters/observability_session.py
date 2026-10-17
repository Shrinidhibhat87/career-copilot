"""One app start: its log file, its session row, and the housekeeping around both."""

from __future__ import annotations

import gzip
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from career_copilot.adapters.store.observability_store import ObservabilityStore
from career_copilot.config import ObservabilitySpec

log = logging.getLogger(__name__)

LOG_PREFIX = "app-"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class AppSession:
    """Start closes what a crash left open and tidies old data; end closes this session.

    Nothing here may stop the app: every step is logged and dropped on failure.
    """

    def __init__(
        self,
        store: ObservabilityStore,
        spec: ObservabilitySpec,
        log_dir: Path,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        profile: str | None = None,
    ) -> None:
        self._store = store
        self._spec = spec
        self._log_dir = log_dir
        self._clock = clock
        self._profile = profile
        self._started = clock()
        stamp = self._started.strftime("%Y%m%dT%H%M%S")
        self.log_path = log_dir / f"{LOG_PREFIX}{stamp}.log"
        self._handler: logging.Handler | None = None
        self._session_id: int | None = None

    def start(self) -> None:
        self._safely(self._close_unclean)
        self._safely(lambda: self._store.roll_up(self._clock(), self._spec.detail_days))
        self._safely(self._delete_old_logs)
        self._safely(self._open_log)
        self._safely(
            lambda: setattr(
                self,
                "_session_id",
                self._store.open_session(self._started, self._profile, str(self.log_path)),
            )
        )

    def end(self) -> None:
        ended = self._clock()
        seconds = (ended - self._started).total_seconds()
        log.info("session end after %ds", int(seconds))
        archive = self._safely(self._archive) if self._spec.archive_logs else None
        self._close_handler()
        if self._session_id is not None:
            session_id = self._session_id
            self._safely(lambda: self._store.close_session(session_id, ended, "clean", archive))

    def _open_log(self) -> None:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self.log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        handler.setLevel(logging.INFO)
        root = logging.getLogger()
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        root.addHandler(handler)
        self._handler = handler
        log.info("session start")

    def _close_handler(self) -> None:
        if self._handler is not None:
            self._handler.flush()
            logging.getLogger().removeHandler(self._handler)
            self._handler.close()
            self._handler = None

    def _archive(self) -> bytes | None:
        if self._handler is not None:
            self._handler.flush()
        return gzip.compress(self.log_path.read_bytes())

    def _close_unclean(self) -> None:
        for session_id, started, path in self._store.unended_sessions():
            try:
                last_write = datetime.fromtimestamp(Path(path).stat().st_mtime, UTC)
            except OSError:
                last_write = started
            self._store.close_session(session_id, last_write, "unclean", None)

    def _delete_old_logs(self) -> None:
        if not self._log_dir.is_dir():
            return
        cutoff = (self._clock() - timedelta(days=self._spec.log_days)).timestamp()
        for path in self._log_dir.glob(f"{LOG_PREFIX}*.log"):
            if path.stat().st_mtime < cutoff:
                path.unlink()

    @staticmethod
    def _safely[T](step: Callable[[], T]) -> T | None:
        try:
            return step()
        except Exception as exc:
            log.warning("observability housekeeping failed: %s", type(exc).__name__)
            return None
