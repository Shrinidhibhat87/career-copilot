"""The Telegram adapter against a fake Bot API (spec 020). No network."""

from __future__ import annotations

import logging

import pytest

from career_copilot.adapters.notify.telegram import TelegramBot
from career_copilot.domain.notifications import Reply
from career_copilot.ports import NotificationError
from tests.telegram_fake import CHAT, TOKEN, FakeTelegram
from tests.unit.test_scoring import make_scored


def bot(fake: FakeTelegram) -> TelegramBot:
    return TelegramBot(TOKEN, CHAT, fake.client())


def test_a_job_is_one_message_with_two_buttons() -> None:
    fake = FakeTelegram()
    bot(fake).send_job(make_scored(7, 88))
    method, params = fake.calls[0]
    assert method == "sendMessage" and params["chat_id"] == CHAT
    buttons = params["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["i:7", "n:7"]
    assert "parse_mode" not in params


def test_presses_from_the_paired_chat_become_replies_and_the_offset_moves() -> None:
    fake = FakeTelegram()
    fake.press(10, "i:7")
    fake.press(11, "n:8")
    replies, offset = bot(fake).poll(0)
    assert [(r.job_id, r.choice) for r in replies] == [(7, "interested"), (8, "not_interested")]
    assert offset == 12


def test_a_press_from_another_chat_is_ignored_but_still_consumed() -> None:
    fake = FakeTelegram()
    fake.press(10, "i:7", chat="777")
    replies, offset = bot(fake).poll(0)
    assert replies == [] and offset == 11


def test_a_press_that_is_not_ours_is_ignored() -> None:
    fake = FakeTelegram()
    fake.press(10, "x:7")
    fake.press(11, "i:notanumber")
    assert bot(fake).poll(0)[0] == []


def test_acknowledging_edits_the_message_and_keeps_the_buttons() -> None:
    fake = FakeTelegram()
    reply = Reply(10, 7, "interested", 10, "cb10", "job text")
    bot(fake).acknowledge(reply, "interested")
    assert fake.methods() == ["answerCallbackQuery", "editMessageText"]
    edit = fake.calls[1][1]
    assert edit["text"].endswith("— Interested") and "reply_markup" in edit


def test_taking_it_back_removes_the_status_line() -> None:
    fake = FakeTelegram()
    reply = Reply(10, 7, "interested", 10, "cb10", "job text\n\n— Interested")
    bot(fake).acknowledge(reply, None)
    assert fake.calls[1][1]["text"] == "job text"


def test_pairing_finds_the_chat_that_sent_the_code() -> None:
    fake = FakeTelegram()
    fake.say(5, "hello")
    fake.say(6, "a1b2c3")
    assert bot(fake).find_chat("a1b2c3") == (CHAT, 7)
    assert bot(fake).find_chat("zzzzzz") is None


def test_a_refused_request_gives_fixed_text_without_the_token() -> None:
    with pytest.raises(NotificationError) as error:
        bot(FakeTelegram(status=401)).send_job(make_scored(1, 80))
    assert error.value.reason == "rejected" and TOKEN not in str(error.value)


def test_a_server_error_is_unreachable() -> None:
    with pytest.raises(NotificationError) as error:
        bot(FakeTelegram(status=502)).deliver("a", "b")
    assert error.value.reason == "unreachable"


def test_the_token_never_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bot(FakeTelegram()).send_job(make_scored(1, 80))
    assert TOKEN not in caplog.text
