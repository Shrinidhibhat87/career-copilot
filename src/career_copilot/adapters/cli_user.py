"""Which account a CLI command works on (spec 015).

`career-copilot --user NAME <command>`; with exactly one account that one is the default.
No passcode: anyone at the shell can already read the files.
"""

from __future__ import annotations

import typer

from career_copilot.adapters import accounts
from career_copilot.config import Config

_user: str | None = None


def select(name: str | None) -> None:
    global _user
    _user = name


def load(config_path: str) -> Config:
    """`Config.load`, pointed at the chosen account's database."""
    config = Config.load(config_path)
    names = accounts.list_accounts(accounts.users_root(config.db_path))
    user = _user or (names[0] if len(names) == 1 else None)
    if user not in names:
        if not names:
            message = "No account yet. Run `career-copilot ui` and create one."
        else:
            message = (
                f"Choose an account: career-copilot --user NAME ... (one of: {', '.join(names)})"
            )
        typer.secho(message, fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return accounts.for_user(config, user)
