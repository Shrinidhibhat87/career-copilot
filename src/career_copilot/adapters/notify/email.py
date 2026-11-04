"""Email digest over the person's own SMTP server (spec 020). Standard library only.

The host, user, password and address are one JSON secret in the keychain. Failures become
fixed messages here: a server's reply can echo a login, so none of it is kept or shown.
"""

from __future__ import annotations

import json
import smtplib
import ssl
from collections.abc import Callable
from dataclasses import asdict, dataclass
from email.message import EmailMessage

from career_copilot.adapters.secrets import ChainedSecretStore
from career_copilot.ports import NotificationError

TIMEOUT_SECONDS = 20
SMTPS_PORT = 465


def email_name(account: str) -> str:
    return f"email_smtp:{account}"


@dataclass(frozen=True, slots=True)
class EmailSettings:
    host: str
    port: int
    user: str
    password: str
    to: str

    def dump(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def load(cls, raw: str | None) -> EmailSettings | None:
        try:
            return cls(**json.loads(raw)) if raw else None
        except (ValueError, TypeError):
            return None


class SmtpMailer:
    def __init__(
        self,
        settings: EmailSettings,
        smtp: Callable[..., smtplib.SMTP] | None = None,
        smtps: Callable[..., smtplib.SMTP_SSL] | None = None,
    ) -> None:
        self._settings = settings
        self._smtp = smtp or smtplib.SMTP
        self._smtps = smtps or smtplib.SMTP_SSL

    def deliver(self, title: str, body: str) -> None:
        s = self._settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = s.user, s.to, title
        message.set_content(body)
        try:
            server: smtplib.SMTP
            if s.port == SMTPS_PORT:
                server = self._smtps(s.host, s.port, timeout=TIMEOUT_SECONDS)
            else:
                server = self._smtp(s.host, s.port, timeout=TIMEOUT_SECONDS)
            with server:
                if s.port != SMTPS_PORT:
                    server.starttls(context=ssl.create_default_context())
                server.login(s.user, s.password)
                server.send_message(message)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused):
            raise NotificationError("mail_rejected") from None
        except (smtplib.SMTPException, OSError):
            raise NotificationError("mail_unreachable") from None


def mailer_for(account: str, secrets: ChainedSecretStore) -> SmtpMailer | None:
    """The mailer for this account, or None until the SMTP details are saved."""
    settings = EmailSettings.load(secrets.get(email_name(account)))
    return SmtpMailer(settings) if settings else None
