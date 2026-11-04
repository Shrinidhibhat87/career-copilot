"""A stand-in for the Telegram Bot API: records every call and answers from a script."""

from __future__ import annotations

import json
from typing import Any

import httpx

TOKEN = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_0123456"
CHAT = "555000999"


class FakeTelegram:
    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.updates: list[dict[str, Any]] = []
        self.status = status

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def press(self, update_id: int, data: str, chat: str = CHAT, text: str = "job text") -> None:
        self.updates.append(
            {
                "update_id": update_id,
                "callback_query": {
                    "id": f"cb{update_id}",
                    "data": data,
                    "message": {"message_id": update_id, "chat": {"id": int(chat)}, "text": text},
                },
            }
        )

    def say(self, update_id: int, text: str, chat: str = CHAT) -> None:
        self.updates.append(
            {"update_id": update_id, "message": {"text": text, "chat": {"id": int(chat)}}}
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        if self.status != 200:
            return httpx.Response(self.status, json={"ok": False})
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "my_bot"}})
        if method == "getUpdates":
            offset = params.get("offset", 0)
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [u for u in self.updates if u["update_id"] >= offset],
                },
            )
        return httpx.Response(200, json={"ok": True, "result": True})
