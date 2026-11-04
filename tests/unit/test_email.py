"""The SMTP mailer against a fake server (spec 020). No network."""

from __future__ import annotations

import smtplib

import pytest

from career_copilot.adapters.notify.email import EmailSettings, SmtpMailer
from career_copilot.domain.notifications import email_body, mask_email
from career_copilot.ports import NotificationError
from tests.smtp_fake import PASSWORD, FakeSmtp
from tests.unit.test_scoring import make_scored

SETTINGS = EmailSettings("smtp.example.com", 587, "me@example.com", PASSWORD, "me@example.com")


def mailer(fake: FakeSmtp, port: int = 587) -> SmtpMailer:
    settings = EmailSettings("smtp.example.com", port, "me@example.com", PASSWORD, "to@example.com")
    return SmtpMailer(settings, fake.factory(), fake.factory(secure=True))


def test_a_digest_is_one_message_to_the_chosen_address() -> None:
    fake = FakeSmtp()
    mailer(fake).deliver("3 new matches", "body")
    assert fake.logins == [("me@example.com", PASSWORD)]
    assert fake.sent[0]["To"] == "to@example.com" and fake.sent[0]["Subject"] == "3 new matches"


def test_port_465_uses_the_ssl_connection() -> None:
    fake = FakeSmtp()
    mailer(fake, port=465).deliver("a", "b")
    assert fake.opened == [("smtp.example.com", 465, True)]


def test_a_wrong_password_is_rejected_without_the_server_reply() -> None:
    fake = FakeSmtp(smtplib.SMTPAuthenticationError(535, PASSWORD.encode()))
    with pytest.raises(NotificationError) as error:
        mailer(fake).deliver("a", "b")
    assert error.value.reason == "mail_rejected" and PASSWORD not in str(error.value)


def test_an_unreachable_host_is_reported_in_fixed_words() -> None:
    with pytest.raises(NotificationError) as error:
        mailer(FakeSmtp(ConnectionRefusedError("smtp.example.com"))).deliver("a", "b")
    assert error.value.reason == "mail_unreachable" and "example" not in str(error.value)


def test_settings_round_trip_and_a_bad_secret_reads_as_none() -> None:
    assert EmailSettings.load(SETTINGS.dump()) == SETTINGS
    assert EmailSettings.load("not json") is None and EmailSettings.load(None) is None


def test_the_address_is_masked_and_the_body_has_every_job_and_the_count() -> None:
    assert mask_email("jane@gmail.com") == "j***@gmail.com"
    body = email_body([make_scored(1, 90), make_scored(2, 80)], 3)
    assert body.count("https://") == 2 and body.endswith("+3 more in the app")
