"""Accounts are folders: the name guard, the passcode check, and adopting the old database."""

from __future__ import annotations

from pathlib import Path

import pytest

from career_copilot.adapters import accounts, cli_user
from career_copilot.config import Config


@pytest.mark.parametrize("name", ["", "..", "a/b", "Alice", "a" * 33, "a b", ".hidden"])
def test_a_bad_name_is_refused_and_makes_no_folder(tmp_path: Path, name: str) -> None:
    with pytest.raises(accounts.AccountError):
        accounts.create(tmp_path / "users", name, "secret1", tmp_path / "jobs.db")
    assert accounts.list_accounts(tmp_path / "users") == []
    assert not (tmp_path / "users").exists() or not any((tmp_path / "users").iterdir())


def test_a_short_passcode_is_refused(tmp_path: Path) -> None:
    with pytest.raises(accounts.AccountError):
        accounts.create(tmp_path / "users", "alice", "short", tmp_path / "jobs.db")


def test_the_right_passcode_verifies_and_nothing_else_does(tmp_path: Path) -> None:
    root = tmp_path / "users"
    accounts.create(root, "alice", "secret1", tmp_path / "jobs.db")
    assert accounts.verify(root, "alice", "secret1")
    assert not accounts.verify(root, "alice", "secret2")
    assert not accounts.verify(root, "nobody", "secret1")
    assert not accounts.verify(root, "../alice", "secret1")
    assert "secret1" not in (root / "alice" / "auth.json").read_text()


def test_a_taken_name_is_reported_as_taken(tmp_path: Path) -> None:
    root = tmp_path / "users"
    accounts.create(root, "alice", "secret1", tmp_path / "jobs.db")
    with pytest.raises(accounts.AccountError) as error:
        accounts.create(root, "alice", "secret1", tmp_path / "jobs.db")
    assert error.value.taken


def test_only_the_first_account_takes_the_old_database(tmp_path: Path) -> None:
    legacy = tmp_path / "jobs.db"
    legacy.write_text("old data")
    (tmp_path / "logs").mkdir()
    root = tmp_path / "users"
    accounts.create(root, "alice", "secret1", legacy)
    assert (root / "alice" / "jobs.db").read_text() == "old data"
    assert (root / "alice" / "logs").is_dir()
    assert not legacy.exists()
    legacy.write_text("newer file at the old path")
    accounts.create(root, "bob", "secret1", legacy)
    assert not (root / "bob" / "jobs.db").exists()
    assert legacy.exists()


def test_for_user_points_the_database_and_digest_into_the_folder(tmp_path: Path) -> None:
    config = accounts.for_user(Config(db_path=str(tmp_path / "jobs.db")), "alice")
    assert config.db_path == str(tmp_path.resolve() / "users" / "alice" / "jobs.db")
    assert config.scoring.digest_path == str(tmp_path.resolve() / "users" / "alice" / "digest.md")


def test_the_cli_defaults_to_the_only_account_and_lists_several(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(f"db_path: {tmp_path / 'jobs.db'}\n")
    root = tmp_path / "users"
    cli_user.select(None)
    with pytest.raises(Exception) as none:
        cli_user.load(str(config))
    assert getattr(none.value, "exit_code", None) == 2
    accounts.create(root, "alice", "secret1", tmp_path / "jobs.db")
    assert cli_user.load(str(config)).db_path.endswith("users/alice/jobs.db")
    accounts.create(root, "bob", "secret1", tmp_path / "jobs.db")
    with pytest.raises(Exception) as several:
        cli_user.load(str(config))
    assert getattr(several.value, "exit_code", None) == 2
    assert "alice, bob" in capsys.readouterr().err
    cli_user.select("bob")
    try:
        assert cli_user.load(str(config)).db_path.endswith("users/bob/jobs.db")
    finally:
        cli_user.select(None)
