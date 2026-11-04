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

from career_copilot.adapters.notify.email import EmailSettings, SmtpMailer, email_name
from career_copilot.adapters.notify.telegram import (
    TOKEN_SHAPE,
    TelegramBot,
    chat_name,
    token_name,
)
from career_copilot.adapters.secrets import ChainedSecretStore, default_secrets
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.notifications import mask_chat, mask_email
from career_copilot.ports import NotificationError
from career_copilot.usecases.notify import CHANNEL, EMAIL, account_of

# One pairing code per account, held in memory: it is only good until the chat answers.
_codes: dict[str, str] = {}


def _error(status: int, field: str, message: str) -> HTTPException:
    return HTTPException(status, detail={"errors": {field: message}})


def router(
    get_config: Callable[[], Config],
    secrets: Callable[[], ChainedSecretStore] = default_secrets,
    bot_for: Callable[[str, str | None], TelegramBot] = TelegramBot,
    mailer_for: Callable[[EmailSettings], SmtpMailer] = SmtpMailer,
) -> APIRouter:
    routes = APIRouter(prefix="/api/notifications", tags=["notifications"])

    def view(config: Config) -> dict[str, Any]:
        account, keys = account_of(config), secrets()
        chat = keys.get(chat_name(account))
        mail = EmailSettings.load(keys.get(email_name(account)))
        with SqliteStore(config.db_path) as store:
            saved = store.notification_setting()
            failure = store.notify_failure(CHANNEL)
            mail_failure = store.notify_failure(EMAIL)
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
            "email": {
                "setUp": mail is not None,
                "on": bool(saved and saved.email),
                "masked": mask_email(mail.to) if mail else None,
                "lastFailure": (
                    {"message": mail_failure[0], "at": mail_failure[1].isoformat()}
                    if mail_failure
                    else None
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
        config = get_config()
        with SqliteStore(config.db_path) as store:
            current = store.notification_setting()
        # A switch left out keeps what was saved, so an older client cannot turn one off.
        enabled = body.get("enabled")
        telegram = body.get("telegram", bool(current and current.telegram))
        email = body.get("email", bool(current and current.email))
        if not (
            isinstance(enabled, bool) and isinstance(telegram, bool) and isinstance(email, bool)
        ):
            raise _error(422, "enabled", "expected true or false")
        keys, account = secrets(), account_of(config)
        if telegram and keys.get(chat_name(account)) is None:
            raise _error(422, "telegram", "set up and pair Telegram before switching it on")
        if email and EmailSettings.load(keys.get(email_name(account))) is None:
            raise _error(422, "email", "set up email before switching it on")
        with SqliteStore(config.db_path) as store:
            store.save_notification_setting(enabled, telegram, email, datetime.now(UTC))
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

    @routes.put("/email")
    def set_email(body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
        """Saves the SMTP details as one keychain secret. Write-only."""
        try:
            settings = EmailSettings(
                host=str(body.get("host", "")).strip(),
                port=int(body.get("port", 0)),
                user=str(body.get("user", "")).strip(),
                password=str(body.get("password", "")),
                # Left empty, the digest goes to the same address it is sent from.
                to=str(body.get("to", "")).strip() or str(body.get("user", "")).strip(),
            )
        except (TypeError, ValueError):
            raise _error(422, "port", "expected a port number") from None
        if not (settings.host and settings.user and settings.password):
            raise _error(422, "host", "host, user and password are required")
        if not 0 < settings.port < 65536:
            raise _error(422, "port", "expected a port number")
        if "@" not in settings.to or "@" not in settings.user:
            raise _error(422, "to", "expected an email address")
        config = get_config()
        secrets().save(email_name(account_of(config)), settings.dump())
        return view(config)

    @routes.delete("/email")
    def remove_email() -> dict[str, Any]:
        config = get_config()
        secrets().delete(email_name(account_of(config)))
        with SqliteStore(config.db_path) as store:
            saved = store.notification_setting()
            if saved and saved.email:
                store.save_notification_setting(
                    saved.enabled, saved.telegram, False, datetime.now(UTC)
                )
            store.clear_notify_failure(EMAIL)
        return view(config)

    @routes.post("/email/test")
    def test_email() -> dict[str, Any]:
        config = get_config()
        settings = EmailSettings.load(secrets().get(email_name(account_of(config))))
        if settings is None:
            raise _error(409, "email", "set up email first")
        try:
            mailer_for(settings).deliver("career-copilot test", "Test message: email is set up.")
        except NotificationError as error:
            return {"ok": False, "error": str(error)}
        return {"ok": True, "error": None}

    return routes
