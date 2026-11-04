"""Telegram Bot API: one message per job with two buttons, and the presses read back by
long polling (spec 020). No webhook, no open port.

The token sits in the request URL, so every failure is turned into a fixed message here and
the HTTP libraries' own request logging is silenced: a token never reaches a log or a stored
error.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from career_copilot.adapters.secrets import ChainedSecretStore
from career_copilot.domain.notifications import (
    LABELS,
    STATUS_MARK,
    Reply,
    telegram_text,
)
from career_copilot.domain.scoring import ScoredJob
from career_copilot.ports import NotificationError

# httpx logs each request URL at INFO, and the URL carries the token.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

API = "https://api.telegram.org"
POLL_SECONDS = 25
TOKEN_SHAPE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{20,}$")
# Prefixes of the button payloads: `i:<job id>` and `n:<job id>`.
_PAYLOAD = {"i": "interested", "n": "not_interested"}


def token_name(account: str) -> str:
    return f"telegram_token:{account}"


def chat_name(account: str) -> str:
    return f"telegram_chat:{account}"


class TelegramBot:
    def __init__(
        self, token: str, chat_id: str | None = None, client: httpx.Client | None = None
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=POLL_SECONDS + 10)

    def _call(self, method: str, **params: Any) -> Any:
        try:
            response = self._client.post(f"{API}/bot{self._token}/{method}", json=params)
        except httpx.HTTPError:
            raise NotificationError("unreachable") from None
        if response.status_code in (400, 401, 403, 404):
            raise NotificationError("rejected")
        if response.status_code != 200:
            raise NotificationError("unreachable")
        body = response.json()
        if not body.get("ok"):
            raise NotificationError("rejected")
        return body["result"]

    def _keyboard(self, job_id: int) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": f"👍 {LABELS['interested']}", "callback_data": f"i:{job_id}"},
                    {"text": f"👎 {LABELS['not_interested']}", "callback_data": f"n:{job_id}"},
                ]
            ]
        }

    def username(self) -> str:
        """Checks the token and names the bot, so the person knows whom to message."""
        return str(self._call("getMe")["username"])

    def deliver(self, title: str, body: str) -> None:
        self._call("sendMessage", chat_id=self._chat_id, text=f"{title}\n{body}".strip())

    def send_job(self, match: ScoredJob) -> None:
        self._call(
            "sendMessage",
            chat_id=self._chat_id,
            text=telegram_text(match),
            reply_markup=self._keyboard(match.job.id),
            disable_web_page_preview=True,
        )

    def find_chat(self, code: str) -> tuple[str, int] | None:
        """The chat that sent `code` to the bot, and the offset past that update."""
        for update in self._call("getUpdates", timeout=0, allowed_updates=["message"]):
            message = update.get("message") or {}
            if (message.get("text") or "").strip() == code:
                return str(message["chat"]["id"]), int(update["update_id"]) + 1
        return None

    def poll(self, offset: int) -> tuple[list[Reply], int]:
        replies: list[Reply] = []
        updates = self._call(
            "getUpdates",
            offset=offset,
            timeout=POLL_SECONDS,
            allowed_updates=["callback_query"],
        )
        for update in updates:
            offset = max(offset, int(update["update_id"]) + 1)
            query = update.get("callback_query") or {}
            message = query.get("message") or {}
            # Only the paired chat counts; anyone else who finds the bot is ignored.
            if str((message.get("chat") or {}).get("id")) != self._chat_id:
                continue
            kind, _, job = str(query.get("data", "")).partition(":")
            if kind not in _PAYLOAD or not job.isdigit():
                continue
            replies.append(
                Reply(
                    int(update["update_id"]),
                    int(job),
                    _PAYLOAD[kind],
                    int(message["message_id"]),
                    str(query["id"]),
                    str(message.get("text", "")),
                )
            )
        return replies, offset

    def acknowledge(self, reply: Reply, shown: str | None) -> None:
        """Stops the button's spinner and writes the choice under the job, keeping the
        buttons so a second press can take it back."""
        self._call("answerCallbackQuery", callback_query_id=reply.callback_id)
        base = reply.text.split(STATUS_MARK)[0]
        text = base + (f"{STATUS_MARK}{LABELS[shown]}" if shown else "")
        self._call(
            "editMessageText",
            chat_id=self._chat_id,
            message_id=reply.message_id,
            text=text,
            reply_markup=self._keyboard(reply.job_id),
            disable_web_page_preview=True,
        )


def telegram_bot(account: str, secrets: ChainedSecretStore) -> TelegramBot | None:
    """The bot for this account, or None until a token is saved and a chat is paired."""
    token = secrets.get(token_name(account))
    chat = secrets.get(chat_name(account))
    return TelegramBot(token, chat) if token and chat else None
