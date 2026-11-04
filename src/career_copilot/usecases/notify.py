"""Push new matches after a run and apply the replies (spec 020).

Off by default: every entry point starts by asking whether the channel is switched on and
set up, and does nothing, with no network call, when it is not.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from career_copilot.adapters.notify.email import mailer_for
from career_copilot.adapters.notify.telegram import TelegramBot, telegram_bot
from career_copilot.adapters.secrets import ChainedSecretStore, default_secrets
from career_copilot.adapters.store.sqlite_store import SqliteStore
from career_copilot.config import Config
from career_copilot.domain.notifications import decision_for, email_body, pick, shown_choice
from career_copilot.domain.runs import RunConfig
from career_copilot.domain.scoring import ScoredJob
from career_copilot.ports import NotificationError, ReplyReceiver

log = logging.getLogger(__name__)
CHANNEL = "telegram"
EMAIL = "email"


def account_of(config: Config) -> str:
    """The account folder name: `data/users/<name>/jobs.db`."""
    return Path(config.db_path).resolve().parent.name


def _active_bot(
    config: Config, store: SqliteStore, secrets: ChainedSecretStore
) -> TelegramBot | None:
    setting = store.notification_setting()
    if not (setting and setting.enabled and setting.telegram):
        return None
    return telegram_bot(account_of(config), secrets)


def _matches(config: Config, store: SqliteStore, frozen: RunConfig) -> list[ScoredJob]:
    # The list in the app caps a remote role abroad far below the bar whatever the model
    # said, so only jobs the app itself would show are worth a message.
    shown = store.kept_job_ids(config.scoring.min_match_score)
    return [
        m
        for m in store.match_scores(
            limit=None,
            model=frozen.match_model,
            rubric_version=frozen.rubric_version,
            max_per_company=frozen.preferences.max_per_company,
            preferences_hash=frozen.preferences_hash,
        )
        if m.job.id in shown and store.current_decision(m.job.id) is None
    ]


def _guarded(
    store: SqliteStore, channel: str, now: Callable[[], datetime], send: Callable[[], None]
) -> None:
    """Runs one channel's send. A failure is recorded in fixed words, never raised."""
    try:
        send()
    except NotificationError as error:
        store.record_notify_failure(channel, str(error), now())
        log.warning("%s push failed: %s", channel, error.reason)
        return
    store.clear_notify_failure(channel)


def notify_after_run(
    config: Config,
    store: SqliteStore,
    frozen: RunConfig,
    now: Callable[[], datetime],
    secrets: ChainedSecretStore | None = None,
) -> None:
    """Sends the best new matches on each channel that is set up and on. A failure is
    recorded and never fails the run."""
    setting = store.notification_setting()
    if not (setting and setting.enabled):
        return
    keys, account = secrets or default_secrets(), account_of(config)
    bot = telegram_bot(account, keys) if setting.telegram else None
    mailer = mailer_for(account, keys) if setting.email else None
    if bot is None and mailer is None:
        return
    matches = _matches(config, store, frozen)

    def telegram() -> None:
        assert bot is not None
        chosen, more = pick(matches, store.notified_job_ids(CHANNEL))
        for match in chosen:
            bot.send_job(match)
            store.mark_notified(match.job.id, CHANNEL, now())
        if more:
            bot.deliver("", f"+{more} more in the app")

    def email() -> None:
        assert mailer is not None
        chosen, more = pick(matches, store.notified_job_ids(EMAIL))
        if not chosen:
            return
        mailer.deliver(f"career-copilot: {len(chosen)} new matches", email_body(chosen, more))
        for match in chosen:
            store.mark_notified(match.job.id, EMAIL, now())

    if bot is not None:
        _guarded(store, CHANNEL, now, telegram)
    if mailer is not None:
        _guarded(store, EMAIL, now, email)


def apply_replies(store: SqliteStore, bot: ReplyReceiver, now: Callable[[], datetime]) -> None:
    """Reads the presses and appends decisions. The offset moves after each press, so a
    press is applied once even if the process stops half way."""
    replies, next_offset = bot.poll(store.telegram_offset())
    for reply in replies:
        current = store.current_decision(reply.job_id)
        append = decision_for(current, reply.choice)
        try:
            if append:
                store.record_decision(reply.job_id, append, "", now())
        except LookupError:
            store.set_telegram_offset(reply.update_id + 1)
            continue
        store.set_telegram_offset(reply.update_id + 1)
        bot.acknowledge(reply, shown_choice(store.current_decision(reply.job_id), reply.choice))
    store.set_telegram_offset(next_offset)
