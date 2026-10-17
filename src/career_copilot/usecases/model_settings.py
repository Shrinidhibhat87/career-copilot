"""The model setup a run uses: the loaded config with the saved model settings applied.

Read at each run start, health check and assistant message, never once at process start,
so a change on the Settings page applies to the next run without a restart (spec 014).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config, resolve_models


def in_force(config: Config, env: Mapping[str, str] | None = None) -> Config:
    with SqliteStore(config.db_path) as store:
        settings = store.model_settings()
    return resolve_models(config, settings, os.environ if env is None else env)
