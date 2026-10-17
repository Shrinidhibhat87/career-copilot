from __future__ import annotations

import pytest

from career_copilot.adapters.secrets import (
    ChainedSecretStore,
    EnvSecretStore,
    KeyringSecretStore,
    tail,
)
from career_copilot.ports import SecretStoreUnavailable


class FakeKeyring:
    priority = 1

    def __init__(self) -> None:
        self.saved: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, name: str) -> str | None:
        return self.saved.get((service, name))

    def set_password(self, service: str, name: str, value: str) -> None:
        self.saved[(service, name)] = value

    def delete_password(self, service: str, name: str) -> None:
        del self.saved[(service, name)]


class NoBackend(FakeKeyring):
    priority = 0  # what `keyring` reports with no real keychain


def chained(env: dict[str, str], backend: FakeKeyring) -> ChainedSecretStore:
    return ChainedSecretStore(EnvSecretStore(env), KeyringSecretStore(backend))


def test_env_beats_the_keychain_and_says_so() -> None:
    backend = FakeKeyring()
    store = chained({"OPENROUTER_API_KEY": "sk-env-1234"}, backend)
    store.set("openrouter", "sk-keychain-5678")
    assert store.get_with_source("openrouter") == ("sk-env-1234", "env")
    assert chained({}, backend).get_with_source("openrouter") == ("sk-keychain-5678", "keychain")
    assert chained({}, FakeKeyring()).get_with_source("openrouter") == (None, "none")


def test_no_keychain_holds_the_key_for_this_session_only() -> None:
    backend = NoBackend()
    store = chained({}, backend)
    assert store.save("openrouter", "sk-test-1234") == "session"
    assert backend.saved == {}
    assert store.get_with_source("openrouter") == ("sk-test-1234", "session")
    assert not store.writable()
    store.delete("openrouter")
    assert store.get("openrouter") is None


def test_the_keychain_refuses_without_a_backend() -> None:
    with pytest.raises(SecretStoreUnavailable, match="no OS keychain"):
        KeyringSecretStore(NoBackend()).set("openrouter", "sk-test-1234")


def test_delete_clears_the_keychain_never_the_env() -> None:
    env = {"OPENROUTER_API_KEY": "sk-env-1234"}
    backend = FakeKeyring()
    store = chained(env, backend)
    store.set("openrouter", "sk-keychain-5678")
    store.delete("openrouter")
    assert backend.saved == {}
    assert env == {"OPENROUTER_API_KEY": "sk-env-1234"}
    store.delete("openrouter")  # nothing left: still fine


def test_tail_shows_four_characters_and_never_a_short_key() -> None:
    assert tail("sk-or-v1-abcdefgh") == "efgh"
    assert tail("short") is None
    assert tail(None) is None
