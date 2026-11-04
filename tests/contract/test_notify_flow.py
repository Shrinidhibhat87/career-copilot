"""Pushing new matches after a run and applying the presses, against a real store (spec 020)."""

from __future__ import annotations

import smtplib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from career_copilot.adapters.notify.email import EmailSettings, email_name
from career_copilot.adapters.notify.telegram import TelegramBot, chat_name, token_name
from career_copilot.adapters.secrets import ChainedSecretStore, EnvSecretStore, MemorySecretStore
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.preferences import Preferences
from career_copilot.domain.runs import RunConfig
from career_copilot.domain.scoring import RUBRIC_VERSION
from career_copilot.usecases.notify import account_of, apply_replies, notify_after_run
from career_copilot.usecases.pipeline import PipelineRun
from tests.contract.test_review_api import score, seed
from tests.smtp_fake import PASSWORD, FakeSmtp
from tests.telegram_fake import CHAT, TOKEN, FakeTelegram

NOW = datetime(2026, 11, 4, 9, tzinfo=UTC)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Config, FakeTelegram]]:
    config = Config(db_path=str(tmp_path / "jobs.db"))
    SqliteStore(config.db_path).close()
    fake = FakeTelegram()
    monkeypatch.setattr(
        "career_copilot.usecases.notify.telegram_bot",
        lambda account, secrets: (
            TelegramBot(TOKEN, CHAT, fake.client())
            if secrets.get(chat_name(account)) and secrets.get(token_name(account))
            else None
        ),
    )
    yield config, fake


def frozen() -> RunConfig:
    return RunConfig(
        preferences=Preferences(),
        triage_model="t",
        match_model="test-model",
        rubric_version=RUBRIC_VERSION,
        mode="incremental",
        budget_eur=0,
        target=10,
        triage_reserve_eur=0,
        match_reserve_eur=0,
        triage_max_output_tokens=100,
        match_max_output_tokens=100,
    )


def keys(config: Config, *, paired: bool = True) -> ChainedSecretStore:
    memory = MemorySecretStore()
    if paired:
        memory.set(token_name(account_of(config)), TOKEN)
        memory.set(chat_name(account_of(config)), CHAT)
    return ChainedSecretStore(EnvSecretStore({}), MemorySecretStore(), memory)


def switch_on(config: Config, *, enabled: bool = True, telegram: bool = True) -> None:
    with SqliteStore(config.db_path) as store:
        store.save_notification_setting(enabled, telegram, False, NOW)


def jobs(config: Config, n: int, value: int = 80, first: int = 0) -> list[int]:
    ids = [
        seed(config, source_job_id=str(i), url=f"https://x.test/{i}", company=f"Company {i}")
        for i in range(first, first + n)
    ]
    for job_id in ids:
        # The run's own preferences hash and model, as the digest reads them.
        score(
            config,
            job_id,
            value,
            preferences_hash=frozen().preferences_hash,
            reasons=("Python matches.",),
        )
    return ids


def push(config: Config, secrets: ChainedSecretStore) -> None:
    with SqliteStore(config.db_path) as store:
        notify_after_run(config, store, frozen(), lambda: NOW, secrets)


def test_a_fresh_account_sends_nothing_and_makes_no_call(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    jobs(config, 3)
    push(config, keys(config))
    assert fake.calls == []


def test_a_set_up_channel_that_is_off_sends_nothing(world: tuple[Config, FakeTelegram]) -> None:
    config, fake = world
    jobs(config, 3)
    switch_on(config, telegram=False)
    push(config, keys(config))
    switch_on(config, enabled=False, telegram=True)
    push(config, keys(config))
    assert fake.calls == []


def test_switched_on_without_pairing_sends_nothing(world: tuple[Config, FakeTelegram]) -> None:
    config, fake = world
    jobs(config, 3)
    switch_on(config)
    push(config, keys(config, paired=False))
    assert fake.calls == []


def test_the_best_five_go_one_message_each_then_a_count(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    jobs(config, 7)
    switch_on(config)
    push(config, keys(config))
    assert fake.methods() == ["sendMessage"] * 6
    assert fake.calls[-1][1]["text"] == "+2 more in the app"


def test_a_second_run_does_not_resend_what_went_out(world: tuple[Config, FakeTelegram]) -> None:
    config, fake = world
    jobs(config, 3)
    switch_on(config)
    push(config, keys(config))
    push(config, keys(config))
    assert fake.methods() == ["sendMessage"] * 3


def test_matches_below_the_bar_and_jobs_already_decided_are_not_sent(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    low = jobs(config, 1, value=10)[0]
    saved = jobs(config, 1, first=5)[0]
    with SqliteStore(config.db_path) as store:
        store.record_decision(saved, "saved", "", NOW)
    assert low != saved
    switch_on(config)
    push(config, keys(config))
    assert fake.calls == []


def test_a_remote_role_abroad_the_app_hides_is_never_pushed(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    abroad, home = jobs(config, 2)
    with SqliteStore(config.db_path) as store:
        store._conn.execute(
            "UPDATE jobs SET filter_warnings = ? WHERE id = ?",
            ("remote role with location outside Germany: United Kingdom", abroad),
        )
        store._conn.commit()
        assert abroad not in store.kept_job_ids(45) and home in store.kept_job_ids(45)
    switch_on(config)
    push(config, keys(config))
    assert [c[1]["text"].count("x.test/1") for c in fake.calls] == [1]


def test_a_failed_send_is_recorded_without_a_secret_and_retried_next_run(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    jobs(config, 2)
    switch_on(config)
    fake.status = 502
    push(config, keys(config))
    with SqliteStore(config.db_path) as store:
        failure = store.notify_failure("telegram")
        assert failure is not None and TOKEN not in failure[0] and CHAT not in failure[0]
        assert store.notified_job_ids("telegram") == set()
    fake.status = 200
    push(config, keys(config))
    with SqliteStore(config.db_path) as store:
        assert len(store.notified_job_ids("telegram")) == 2
        assert store.notify_failure("telegram") is None


def test_a_press_saves_and_a_second_press_takes_it_back(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    job = jobs(config, 1)[0]
    bot = TelegramBot(TOKEN, CHAT, fake.client())
    fake.press(10, f"i:{job}")
    with SqliteStore(config.db_path) as store:
        apply_replies(store, bot, lambda: NOW)
        assert store.current_decision(job) == "saved"
        fake.press(11, f"i:{job}")
        apply_replies(store, bot, lambda: NOW)
        assert store.current_decision(job) is None


def test_the_other_button_replaces_the_choice_and_dismiss_goes_to_the_bin(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    job = jobs(config, 1)[0]
    bot = TelegramBot(TOKEN, CHAT, fake.client())
    fake.press(10, f"i:{job}")
    fake.press(11, f"n:{job}")
    with SqliteStore(config.db_path) as store:
        apply_replies(store, bot, lambda: NOW)
        assert store.current_decision(job) == "dismissed" and store.in_bin(job)


def test_a_press_is_applied_once_even_if_polled_again(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    job = jobs(config, 1)[0]
    bot = TelegramBot(TOKEN, CHAT, fake.client())
    fake.press(10, f"i:{job}")
    with SqliteStore(config.db_path) as store:
        apply_replies(store, bot, lambda: NOW)
        apply_replies(store, bot, lambda: NOW)
        assert store.current_decision(job) == "saved" and store.telegram_offset() == 11
        assert fake.methods().count("editMessageText") == 1


def test_a_press_from_a_stranger_changes_nothing(world: tuple[Config, FakeTelegram]) -> None:
    config, fake = world
    job = jobs(config, 1)[0]
    bot = TelegramBot(TOKEN, CHAT, fake.client())
    fake.press(10, f"n:{job}", chat="777")
    with SqliteStore(config.db_path) as store:
        apply_replies(store, bot, lambda: NOW)
        assert store.current_decision(job) is None


def test_a_press_for_a_cleaned_job_is_dropped_not_fatal(
    world: tuple[Config, FakeTelegram],
) -> None:
    config, fake = world
    bot = TelegramBot(TOKEN, CHAT, fake.client())
    fake.press(10, "i:9999")
    with SqliteStore(config.db_path) as store:
        apply_replies(store, bot, lambda: NOW)
        assert store.telegram_offset() == 11


def test_a_crash_while_pushing_never_changes_the_run(
    world: tuple[Config, FakeTelegram], monkeypatch: pytest.MonkeyPatch
) -> None:
    config, _ = world

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("career_copilot.usecases.pipeline.notify_after_run", boom)
    with SqliteStore(config.db_path) as store:
        PipelineRun(store, config, 1)._push(frozen())  # must not raise


# --- email ---------------------------------------------------------------------------------


def mail_keys(config: Config) -> ChainedSecretStore:
    memory = MemorySecretStore()
    settings = EmailSettings("smtp.example.com", 587, "me@example.com", PASSWORD, "to@example.com")
    memory.set(email_name(account_of(config)), settings.dump())
    return ChainedSecretStore(EnvSecretStore({}), MemorySecretStore(), memory)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> FakeSmtp:
    fake = FakeSmtp()
    monkeypatch.setattr("smtplib.SMTP", fake.factory())
    return fake


def switch_mail(config: Config, *, email: bool = True) -> None:
    with SqliteStore(config.db_path) as store:
        store.save_notification_setting(True, False, email, NOW)


def test_email_sends_one_digest_per_run_with_the_cap(
    world: tuple[Config, FakeTelegram], smtp: FakeSmtp
) -> None:
    config, fake = world
    jobs(config, 7)
    switch_mail(config)
    push(config, mail_keys(config))
    assert len(smtp.sent) == 1 and fake.calls == []
    body = smtp.sent[0].get_content()
    assert body.count("https://x.test") == 5 and "+2 more in the app" in body


def test_email_does_not_resend_and_sends_nothing_when_nothing_is_new(
    world: tuple[Config, FakeTelegram], smtp: FakeSmtp
) -> None:
    config, _ = world
    jobs(config, 2)
    switch_mail(config)
    push(config, mail_keys(config))
    push(config, mail_keys(config))
    assert len(smtp.sent) == 1


def test_email_is_silent_when_off_or_not_set_up(
    world: tuple[Config, FakeTelegram], smtp: FakeSmtp
) -> None:
    config, _ = world
    jobs(config, 2)
    switch_mail(config, email=False)
    push(config, mail_keys(config))
    switch_mail(config)
    push(config, keys(config, paired=False))
    assert smtp.sent == [] and smtp.opened == []


def test_a_failed_email_is_recorded_and_retried_and_telegram_still_goes(
    world: tuple[Config, FakeTelegram], smtp: FakeSmtp
) -> None:
    config, _ = world
    jobs(config, 2)
    with SqliteStore(config.db_path) as store:
        store.save_notification_setting(True, True, True, NOW)
    both = mail_keys(config)
    both.set(token_name(account_of(config)), TOKEN)
    both.set(chat_name(account_of(config)), CHAT)
    smtp.fail = smtplib.SMTPAuthenticationError(535, PASSWORD.encode())
    push(config, both)
    with SqliteStore(config.db_path) as store:
        failure = store.notify_failure("email")
        assert failure is not None and PASSWORD not in failure[0]
        assert store.notified_job_ids("email") == set()
        assert len(store.notified_job_ids("telegram")) == 2
    smtp.fail = None
    push(config, both)
    assert len(smtp.sent) == 1
