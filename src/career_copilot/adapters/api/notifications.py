"""HTTP for pushing matches to Telegram (spec 020). A driving adapter only.

Off by default: nothing is set up and nothing is on until the person says so. A token and a
chat id go to the keychain and are never returned; the page gets a masked value.
"""

from __future__ import annotations

import secrets as randomness
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException

from career_copilot.adapters.notify.telegram import (
    TOKEN_SHAPE,
    TelegramBot,
    chat_name,
    token_name,
)
from career_copilot.adapters.secrets import ChainedSecretStore, default_secrets
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.notifications import mask_chat
from career_copilot.ports import NotificationError
from career_copilot.usecases.notify import CHANNEL, account_of

# One pairing code per account, held in memory: it is only good until the chat answers.
_codes: dict[str, str] = {}


def _error(status: int, field: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"errors": {field: message}})


def router(
    get_config: Callable[[], Config],
    secrets: Callable[[], ChainedSecretStore] = default_secrets,
    bot_for: Callable[[str, str | None], TelegramBot] = TelegramBot,
) -> APIRouter:
    routes = APIRouter(prefix="/api/notifications", tags=["notifications"])

    def view(config: Config) -> dict[str, Any]:
        account, keys = account_of(config), secrets()
        chat = keys.get(chat_name(account))
        with SqliteStore(config.db_path) as store:
            saved = store.notification_setting()
            failure = store.notify_failure(CHANNEL)
        return {
            "enabled": bool(saved and saved.enabled),
            "keychain": keys.writable(),
            "telegram": {
                "tokenSet": keys.get(token_name(account)) is not None,
                "paired": chat is not None,
                "on": bool(saved and saved.telegram),
                "masked": mask_chat(chat) if chat else None,
                "lastFailure": (
                    {"message": failure[0], "at": failure[1].isoformat()} if failure else None
                ),
            },
        }

    def bot(config: Config) -> TelegramBot:
        account, keys = account_of(config), secrets()
        token, chat = keys.get(token_name(account)), keys.get(chat_name(account))
        if not token:
            raise _error(409, "telegram", "set up Telegram first")
        return bot_for(token, chat)

    @routes.get("")
    def get() -> dict[str, Any]:
        return view(get_config())

    @routes.put("")
    def put(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        enabled, telegram = body.get("enabled"), body.get("telegram")
        if not isinstance(enabled, bool) or not isinstance(telegram, bool):
            raise _error(422, "enabled", "expected true or false")
        config = get_config()
        if telegram and secrets().get(chat_name(account_of(config))) is None:
            raise _error(422, "telegram", "set up and pair Telegram before switching it on")
        with SqliteStore(config.db_path) as store:
            store.save_notification_setting(enabled, telegram, False, datetime.now(UTC))
        return view(config)

    @routes.put("/telegram")
    def set_token(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Saves the bot token and starts pairing. The token is write-only."""
        token = str(body.get("token", "")).strip()
        if not TOKEN_SHAPE.match(token):
            raise _error(422, "token", "that does not look like a bot token")
        config = get_config()
        try:
            username = bot_for(token, None).username()
        except NotificationError as error:
            raise _error(422, "token", str(error)) from None
        account, keys = account_of(config), secrets()
        keys.save(token_name(account), token)
        keys.delete(chat_name(account))
        code = _codes[account] = randomness.token_hex(3)
        return {"code": code, "bot": username}

    @routes.get("/telegram/pairing")
    def pairing() -> dict[str, Any]:
        """Looks once for the code in the bot's messages; the page asks every few seconds."""
        config = get_config()
        account, keys = account_of(config), secrets()
        if keys.get(chat_name(account)) is None and (code := _codes.get(account)):
            try:
                found = bot(config).find_chat(code)
            except NotificationError as error:
                raise _error(502, "telegram", str(error)) from None
            if found:
                keys.save(chat_name(account), found[0])
                with SqliteStore(config.db_path) as store:
                    store.set_telegram_offset(found[1])
                _codes.pop(account, None)
        return view(config)["telegram"]  # type: ignore[no-any-return]

    @routes.delete("/telegram")
    def remove() -> dict[str, Any]:
        config = get_config()
        account, keys = account_of(config), secrets()
        keys.delete(token_name(account))
        keys.delete(chat_name(account))
        _codes.pop(account, None)
        with SqliteStore(config.db_path) as store:
            saved = store.notification_setting()
            if saved and saved.telegram:
                store.save_notification_setting(
                    saved.enabled, False, saved.email, datetime.now(UTC)
                )
            store.clear_notify_failure(CHANNEL)
        return view(config)

    @routes.post("/telegram/test")
    def test() -> dict[str, Any]:
        config = get_config()
        try:
            bot(config).deliver("career-copilot", "Test message: Telegram is set up.")
        except NotificationError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "error": None}

    return routes
