"""API keys from the environment first, then the OS keychain (spec 014).

The environment wins so a developer's `.env` keeps working; the keychain is where the
Settings page puts a key. Nothing here ever writes a key to disk itself: on a machine with
no keychain backend (headless Linux, WSL) a saved key is held in this process's memory
only, until the server stops, rather than falling back to a plain-text file.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Literal

from career_copilot.ports import SecretStore, SecretStoreUnavailable

SERVICE = "career-copilot"
OPENROUTER = "openrouter"
# Which environment variable stands in for each secret.
ENV_NAMES: Mapping[str, str] = {OPENROUTER: "OPENROUTER_API_KEY"}

Source = Literal["env", "keychain", "session", "none"]

NO_KEYCHAIN = "no OS keychain; put OPENROUTER_API_KEY in .env"


def secret_for_env(env_name: str) -> str | None:
    """The secret an environment variable stands for, e.g. OPENROUTER_API_KEY -> openrouter."""
    return next((name for name, env in ENV_NAMES.items() if env == env_name), None)


class EnvSecretStore:
    """Read-only: the app never edits `.env` or the shell."""

    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    def get(self, name: str) -> str | None:
        env_name = ENV_NAMES.get(name)
        value = self._env.get(env_name, "").strip() if env_name else ""
        return value or None

    def set(self, name: str, value: str) -> None:
        raise SecretStoreUnavailable("the environment is read-only")

    def delete(self, name: str) -> None:
        raise SecretStoreUnavailable("the environment is read-only")


class KeyringSecretStore:
    """macOS Keychain, Windows Credential Manager, or Secret Service, through `keyring`."""

    def __init__(self, backend: Any | None = None) -> None:
        self._backend = backend

    def _keyring(self) -> Any:
        if self._backend is not None:
            return self._backend
        import keyring  # imported lazily: start-up must not wait on a keychain daemon

        return keyring.get_keyring()

    def _usable(self) -> Any:
        backend = self._keyring()
        # `keyring` answers with a "fail" backend (priority 0) when nothing real is there.
        if getattr(backend, "priority", 1) <= 0:
            raise SecretStoreUnavailable(NO_KEYCHAIN)
        return backend

    def available(self) -> bool:
        try:
            self._usable()
        except Exception:
            return False
        return True

    def get(self, name: str) -> str | None:
        try:
            value = self._usable().get_password(SERVICE, name)
        except Exception:
            return None
        return value or None

    def set(self, name: str, value: str) -> None:
        backend = self._usable()
        try:
            backend.set_password(SERVICE, name, value)
        except Exception as exc:
            raise SecretStoreUnavailable(f"{NO_KEYCHAIN} ({type(exc).__name__})") from None

    def delete(self, name: str) -> None:
        try:
            backend = self._usable()
        except SecretStoreUnavailable:
            return  # no keychain, so nothing was ever stored in one
        if backend.get_password(SERVICE, name) is None:
            return
        try:
            backend.delete_password(SERVICE, name)
        except Exception as exc:
            raise SecretStoreUnavailable(
                f"could not clear the keychain ({type(exc).__name__})"
            ) from None


class MemorySecretStore:
    """Held for the life of the process. Gone when the server stops; never on disk."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)


# One per server process, so a key saved on the Settings page reaches the run worker.
_SESSION = MemorySecretStore()


class ChainedSecretStore:
    """Env, then keychain, then this session. Writes go to the keychain, or to the session
    when there is no keychain; deletes clear both. The env is never written."""

    def __init__(
        self, env: SecretStore, keychain: SecretStore, session: SecretStore | None = None
    ) -> None:
        self._env = env
        self._keychain = keychain
        self._session = session or MemorySecretStore()

    def get_with_source(self, name: str) -> tuple[str | None, Source]:
        value = self._env.get(name)
        if value:
            return value, "env"
        value = self._keychain.get(name)
        if value:
            return value, "keychain"
        value = self._session.get(name)
        if value:
            return value, "session"
        return None, "none"

    def get(self, name: str) -> str | None:
        return self.get_with_source(name)[0]

    def writable(self) -> bool:
        """Whether a key outlives this process here: a keychain backend exists."""
        available = getattr(self._keychain, "available", None)
        return bool(available()) if callable(available) else True

    def save(self, name: str, value: str) -> Source:
        """Store a key and say where it went: the keychain, or this session only."""
        try:
            self._keychain.set(name, value)
        except SecretStoreUnavailable:
            self._session.set(name, value)
            return "session"
        self._session.delete(name)
        return "keychain"

    def set(self, name: str, value: str) -> None:
        self.save(name, value)

    def delete(self, name: str) -> None:
        self._session.delete(name)
        self._keychain.delete(name)


def default_secrets() -> ChainedSecretStore:
    return ChainedSecretStore(EnvSecretStore(), KeyringSecretStore(), _SESSION)


def tail(value: str | None) -> str | None:
    """The last four characters: enough to tell two keys apart, never enough to use one."""
    return value[-4:] if value and len(value) > 8 else None
