# Telegram and Email Digest — Design

> Lets a person who wants it get new matches on their phone (Telegram) and in their inbox (email) after a scheduled run. Nothing is sent unless the person has set a channel up and switched it on.
> Core rule: nothing is configured and nothing is sent by default; the contact details and credentials are PII and secrets and live only in the OS keychain.
> Builds on spec 014 (secrets), spec 015 (accounts) and spec 019 (scheduler).

## Decisions

| Topic | Decision |
|---|---|
| The gap | Results show only inside the app. A scheduled run (spec 019) finishes with nobody looking. The `Notifier` port exists, with a Markdown file as its only adapter. |
| Off by default | A new account has no channel set up and sends nothing. Running a job never sends a message unless a channel was set up and switched on. The app is fully usable with no token, address or SMTP set. Every send path starts by asking whether the channel is set up and on, and does nothing, with no network call, when it is not. |
| Opt in | The person sets a channel up in Settings, then switches it on. Switching it off keeps its credentials; Remove deletes them from the keychain. A master switch turns all pushing off at once. |
| PII and secrets | Bot token, Telegram chat ID, email address, SMTP host, user and password go to the OS keychain through the `SecretStore` of spec 014, keyed per account. Never `jobs.db`, `config.yaml`, account exports, logs, observability or any model call. The API never returns a secret; it returns a masked value (`j***@gmail.com`, `chat …4821`). With no keychain a value lives in memory until the server stops, as the API key does today. |
| Redactor | Not applied to messages. It guards calls to model providers, and stored reasons are already restored text. Telegram and the mail provider do hold what is sent, so Settings says so in one sentence. Reasons go as stored. |
| Content | Score, the reasons, and the posting's own link (never a localhost link). Only jobs above `scoring.min_match_score` that were never sent on that channel. |
| When | After any run that finishes, scheduled or manual, when the master switch and a channel are on. A manual run notifies too, which is also how a person tests the setup. With nothing on, neither sends. |
| Cap | At most 5 jobs per run, best first, then one line `+N more in the app`. |
| Telegram | One message per job with two buttons, 👍 Interested and 👎 Not interested. 👍 saves the job (✓), 👎 dismisses it (✕, recoverable from the recycle bin). The message is edited to show the choice and keeps its buttons. |
| Replies | Long-polling `getUpdates` while the account is signed in, started with the scheduler's session. No webhook, no open port, no public URL. Updates from any chat other than the paired one are ignored. The update offset is stored so a reply applies once. Replies are idempotent, because decisions are an append-only log where the latest wins: pressing the button for the choice already in force appends nothing; pressing the other button replaces it; pressing the chosen button again takes it back, which appends `restored` (no decision), the same as a job returning from the recycle bin, and the message goes back to its two plain buttons. A reply sent while the app was closed is applied at the next sign-in (Telegram keeps it about 24 hours). |
| Pairing | The person creates a bot with BotFather and pastes its token. The app shows a one-time code to send to the bot, then stores the chat ID itself. The person never types a chat ID. |
| Email | The person's own SMTP: host, port, user, app password, recipient. One digest email per run, same cap, each job with score, reasons and link. No reply handling. |
| Failures | A failed send never fails the run. It is recorded as a category and a message with no secret in it, shown in Settings, and retried at the next scheduled run. Each channel has a Test button that sends a harmless test message. |
| Doctor | One line per channel: `telegram: paired (chat …4821, on)`, `email: off`. An unset channel is `off`, never a failure. |

## Shapes

```python
# domain/notifications.py: pure, no I/O.
def pick(matches: Sequence[Match], sent: set[str], cap: int = 5) -> tuple[list[Match], int]: ...  # (to send, how many more)
def mask_email(value: str) -> str: ...        # j***@gmail.com
def mask_chat(value: str) -> str: ...         # chat …4821
def telegram_text(match: Match) -> str: ...
def email_body(matches: Sequence[Match], more: int) -> str: ...
```

```python
# ports.py: Notifier stays; one new port for replies.
class ReplyReceiver(Protocol):
    def poll(self) -> list[Reply]: ...        # Reply(job_id, choice: "interested" | "not_interested")
```

```sql
-- Migration 37. No PII anywhere in these tables.
CREATE TABLE notification_settings (
    version  INTEGER PRIMARY KEY AUTOINCREMENT,
    enabled  INTEGER NOT NULL,        -- master switch
    telegram INTEGER NOT NULL,        -- channel switched on
    email    INTEGER NOT NULL,
    saved_at TEXT NOT NULL
);
CREATE TABLE notified_jobs (
    job_id  TEXT NOT NULL,
    channel TEXT NOT NULL,            -- telegram | email
    sent_at TEXT NOT NULL,
    PRIMARY KEY (job_id, channel)
);
CREATE TABLE telegram_state (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    offset INTEGER NOT NULL
);
```

```
GET  /api/notifications → {enabled, telegram: {setUp, on, masked, lastFailure}, email: {setUp, on, masked, lastFailure}, keychain: bool}
PUT  /api/notifications {enabled, telegram: bool, email: bool} → same shape; refused when a channel is switched on without being set up
PUT  /api/notifications/telegram {token}       → starts pairing, returns {code}; write-only
GET  /api/notifications/telegram/pairing       → {paired: bool, masked}
PUT  /api/notifications/email {host, port, user, password, to}   → write-only
DELETE /api/notifications/{channel}            → removes the credentials
POST /api/notifications/{channel}/test         → {ok, error: category | null}
```

## Flow

1. `/settings` has a Notifications card built like the Schedule card: collapsed by default, a master switch, a Telegram card and an Email card each with its own switch. An unset channel shows "Not set up" and a Set up button that opens a modal for the token and pairing, or for the SMTP fields. Once saved the card shows the masked value, Test and Remove.
2. Pairing: the person pastes the token, the app shows a code, the person sends it to the bot, the app sees it through `getUpdates`, stores the chat ID and shows the masked value.
3. A run finishes, scheduled or manual. If the master switch and a channel are on, the new matches above the bar that were never sent on that channel are picked, capped at 5.
4. Telegram sends one message per job with the two buttons. Email sends one digest. Each success writes `notified_jobs`; each failure is recorded and the run is untouched.
5. While signed in, the reply poller reads button presses from the paired chat only, applies save or dismiss through the existing lifecycle, edits the message, and stores the offset.
6. A switched-off or unset channel, or the master switch off, does nothing and opens no connection.

## Testing

- Pure: `pick` for the cap, the `+N more` count and jobs already sent; masks for short and odd values; message text carries score, reasons and the posting link and no CV text beyond the stored reasons.
- Store: migration 37 on an existing database; versions round-trip, newest wins; `notified_jobs` is unique per job and channel; the offset round-trips.
- Default state: a fresh account shows "Not set up" and sends nothing; a set-up channel that is off sends nothing; with nothing on, no network call is made.
- Telegram, against a fake Bot API: one message per job, buttons present, 👍 saves, 👎 dismisses, the message is edited, an update from another chat is ignored, the offset keeps a reply from applying twice, a repeated 👍 appends nothing, 👎 after 👍 replaces it, tapping the chosen button again takes it back to no decision and restores the plain buttons, pairing stores the chat ID.
- Email, against a fake SMTP server: one message per run with the cap line, a wrong password and an unreachable host give a failure category and no secret in the text.
- Leaks: no secret or masked source value in `jobs.db`, account exports, API responses, log output or error text.
- Wiring: a scheduled run sends, a manual run sends too when a channel is on, and neither sends with nothing on, a failed send leaves the run `done`, a second run does not resend.
- API contract: shape, write-only credentials, switching a channel on while it is not set up is refused.
- By hand, after the UI branch (needs the person's own bot token and SMTP app password, one test message each, with their go-ahead before any live call): set Telegram up, pair, send a test, then run a job by hand and see the messages arrive, tap 👍, 👎 and 👍 again to take it back, and check the app agrees each time; set email up and send a test; remove each and check nothing remains.

## Non-goals

Quiet hours (the schedule slots already choose the time), Telegram commands beyond the two buttons, a webhook or public URL, email replies, other channels (Slack, push), per-channel thresholds, sending while signed out, and passing message text through the PII redactor.

## Delivery

One spec, four small branches, merged in this order:

1. `docs/notifications-spec` (this spec)
2. `feat/telegram-digest` (secrets, migration 37, pairing, per-job messages, reply poller, API, doctor line)
3. `feat/email-digest` (SMTP sender, digest email, API, doctor line)
4. `feat/notifications-ui` (Notifications card on `/settings`, set-up modals, test and remove buttons)
