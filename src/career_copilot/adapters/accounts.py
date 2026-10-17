"""Local accounts: one folder per person under `data/users/` (spec 015).

The folders are the account list; there is no registry. A person's `jobs.db`, logs and
`auth.json` live in their folder. The passcode guards the app, not the file.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
from pathlib import Path

from career_copilot.config import Config

NAME = re.compile(r"^[a-z0-9_-]{1,32}$")
MIN_PASSCODE = 6
_AUTH = "auth.json"
# scrypt's cost parameters, kept in auth.json so they can change without locking anyone out.
_COST = {"n": 2**14, "r": 8, "p": 1}


class AccountError(ValueError):
    """A name or passcode that cannot make an account, or a name already taken."""

    def __init__(self, message: str, *, taken: bool = False) -> None:
        super().__init__(message)
        self.taken = taken


def users_root(db_path: str) -> Path:
    """`data/users`, beside the legacy `data/jobs.db` the config still names."""
    return Path(db_path).resolve().parent / "users"


def list_accounts(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if (p / _AUTH).is_file())


def _hash(passcode: str, salt: bytes, cost: dict[str, int]) -> bytes:
    return hashlib.scrypt(passcode.encode(), salt=salt, **cost)


def has_legacy_data(legacy_db: Path) -> bool:
    return legacy_db.is_file()


def create(root: Path, name: str, passcode: str, legacy_db: Path) -> None:
    """Make the account. The first one adopts the pre-accounts `data/jobs.db`."""
    if not NAME.fullmatch(name):
        raise AccountError("Use 1-32 lowercase letters, digits, - or _ for the username.")
    if len(passcode) < MIN_PASSCODE:
        raise AccountError(f"The passcode needs at least {MIN_PASSCODE} characters.")
    first = not list_accounts(root)
    folder = root / name
    try:
        folder.mkdir(parents=True)
    except FileExistsError:
        raise AccountError("That username is taken.", taken=True) from None
    salt = os.urandom(16)
    record = {"salt": salt.hex(), "hash": _hash(passcode, salt, _COST).hex(), **_COST}
    if first and legacy_db.is_file():
        _adopt(legacy_db, folder)
    (folder / _AUTH).write_text(json.dumps(record))


def _adopt(legacy_db: Path, folder: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{legacy_db}{suffix}")
        if source.exists():
            shutil.move(source, folder / f"jobs.db{suffix}")
    logs = legacy_db.parent / "logs"
    if logs.is_dir():
        shutil.move(logs, folder / "logs")


def verify(root: Path, name: str, passcode: str) -> bool:
    """False for a wrong passcode and for an unknown name alike."""
    if not NAME.fullmatch(name):
        return False
    try:
        record = json.loads((root / name / _AUTH).read_text())
        cost = {key: record[key] for key in _COST}
        expected = bytes.fromhex(record["hash"])
        actual = _hash(passcode, bytes.fromhex(record["salt"]), cost)
    except (OSError, ValueError, KeyError):
        return False
    return hmac.compare_digest(expected, actual)


def for_user(config: Config, name: str) -> Config:
    """The config with the database and digest moved into this person's folder."""
    folder = users_root(config.db_path) / name
    return config.model_copy(
        update={
            "db_path": str(folder / "jobs.db"),
            "scoring": config.scoring.model_copy(update={"digest_path": str(folder / "digest.md")}),
        }
    )
