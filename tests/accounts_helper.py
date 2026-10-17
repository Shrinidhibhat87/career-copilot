"""CLI tests run as the one account `test`, whose database sits under `tmp_path/users/test`."""

from __future__ import annotations

from pathlib import Path

from career_copilot.adapters import accounts


def make_account(tmp_path: Path) -> str:
    """Create the account and return the path of its database."""
    accounts.create(tmp_path / "users", "test", "secret1", tmp_path / "jobs.db")
    return str(tmp_path / "users" / "test" / "jobs.db")
