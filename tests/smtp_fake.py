"""A stand-in for smtplib: records the login and the messages, and can fail on demand."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from types import TracebackType
from typing import Any

PASSWORD = "app-password-123"


class FakeSmtp:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.sent: list[EmailMessage] = []
        self.logins: list[tuple[str, str]] = []
        self.opened: list[tuple[str, int, bool]] = []

    def factory(self, secure: bool = False) -> Any:
        def make(host: str, port: int, timeout: int) -> FakeSmtp:
            self.opened.append((host, port, secure))
            if isinstance(self.fail, OSError):
                raise self.fail
            return self

        return make

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def starttls(self, context: object = None) -> None:
        return None

    def login(self, user: str, password: str) -> None:
        if isinstance(self.fail, smtplib.SMTPAuthenticationError):
            raise self.fail
        self.logins.append((user, password))

    def send_message(self, message: EmailMessage) -> None:
        self.sent.append(message)
