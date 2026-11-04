"""Telegram setup over HTTP and in the store (spec 020). Off by default; secrets never leave."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from career_copilot.adapters.api import notifications
from career_copilot.adapters.notify.telegram import TelegramBot, chat_name, token_name
from career_copilot.adapters.secrets import ChainedSecretStore, EnvSecretStore, MemorySecretStore
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.usecases.notify import account_of
from tests.telegram_fake import CHAT, TOKEN, FakeTelegram


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[TestClient, Config, FakeTelegram, ChainedSecretStore]]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    SqliteStore(config.db_path).close()
    fake = FakeTelegram()
    keys = ChainedSecretStore(EnvSecretStore({}), MemorySecretStore(), MemorySecretStore())
    app = FastAPI()
    app.include_router(
        notifications.router(
            lambda: config,
            lambda: keys,
            lambda token, chat: TelegramBot(token, chat, fake.client()),
        )
    )
    yield TestClient(app), config, fake, keys


World = tuple[TestClient, Config, FakeTelegram, ChainedSecretStore]


def pair(world: World) -> None:
    client, _, fake, _ = world
    code = client.put("/api/notifications/telegram", json={"token": TOKEN}).json()["code"]
    fake.say(3, code)
    client.get("/api/notifications/telegram/pairing")


def test_a_fresh_account_has_nothing_set_up_and_nothing_on(world: World) -> None:
    client, _, fake, _ = world
    body = client.get("/api/notifications").json()
    assert body["enabled"] is False
    assert body["telegram"] == {
        "tokenSet": False,
        "paired": False,
        "on": False,
        "masked": None,
        "lastFailure": None,
    }
    assert fake.calls == []


def test_it_cannot_be_switched_on_before_it_is_paired(world: World) -> None:
    client, *_ = world
    refused = client.put("/api/notifications", json={"enabled": True, "telegram": True})
    assert refused.status_code == 422
    assert client.get("/api/notifications").json()["telegram"]["on"] is False


def test_a_token_that_is_not_a_token_is_refused_without_a_call(world: World) -> None:
    client, _, fake, _ = world
    assert client.put("/api/notifications/telegram", json={"token": "hello"}).status_code == 422
    assert fake.calls == []


def test_a_token_telegram_refuses_is_reported_and_not_kept(world: World) -> None:
    client, _, fake, keys = world
    fake.status = 401
    response = client.put("/api/notifications/telegram", json={"token": TOKEN})
    assert response.status_code == 422 and TOKEN not in response.text
    assert keys.get(token_name("x")) is None


def test_pairing_stores_the_chat_in_the_keychain_and_shows_it_masked(world: World) -> None:
    client, config, _, keys = world
    pair(world)
    body = client.get("/api/notifications").json()["telegram"]
    assert body["paired"] is True and body["masked"] == f"chat …{CHAT[-4:]}"
    assert keys.get(chat_name(account_of(config))) == CHAT


def test_nothing_is_paired_until_the_code_is_sent(world: World) -> None:
    client, *_ = world
    client.put("/api/notifications/telegram", json={"token": TOKEN})
    assert client.get("/api/notifications/telegram/pairing").json()["paired"] is False


def test_switching_on_after_pairing_takes_effect_and_is_remembered(world: World) -> None:
    client, *_ = world
    pair(world)
    body = client.put("/api/notifications", json={"enabled": True, "telegram": True}).json()
    assert body["enabled"] is True and body["telegram"]["on"] is True


def test_the_test_button_sends_one_message(world: World) -> None:
    client, _, fake, _ = world
    pair(world)
    fake.calls.clear()
    assert client.post("/api/notifications/telegram/test").json() == {"ok": True, "error": None}
    assert fake.methods() == ["sendMessage"]


def test_a_failing_test_says_why_in_fixed_words(world: World) -> None:
    client, _, fake, _ = world
    pair(world)
    fake.status = 502
    body = client.post("/api/notifications/telegram/test").json()
    assert body["ok"] is False and TOKEN not in body["error"]


def test_remove_deletes_the_secrets_and_switches_it_off(world: World) -> None:
    client, config, _, keys = world
    pair(world)
    client.put("/api/notifications", json={"enabled": True, "telegram": True})
    body = client.delete("/api/notifications/telegram").json()
    assert body["telegram"]["tokenSet"] is False and body["telegram"]["on"] is False
    assert keys.get(token_name(account_of(config))) is None


def test_no_secret_reaches_the_database_or_any_response(world: World, tmp_path: Path) -> None:
    client, config, _, _ = world
    pair(world)
    seen = [
        client.get("/api/notifications").text,
        client.put("/api/notifications", json={"enabled": True, "telegram": True}).text,
        client.get("/api/notifications/telegram/pairing").text,
    ]
    assert not any(TOKEN in text or CHAT in text for text in seen)
    dump = "\n".join(sqlite3.connect(config.db_path).iterdump())
    assert TOKEN not in dump and CHAT not in dump
