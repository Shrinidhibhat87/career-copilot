"use client";

import { useEffect, useState, type FormEvent } from "react";

import { Disclosure } from "@/components/disclosure";
import { Modal } from "@/components/modal";
import {
  getNotifications,
  getPairing,
  removeEmail,
  removeTelegram,
  saveEmail,
  saveNotifications,
  setTelegramToken,
  testEmail,
  testTelegram,
  type NotificationsView,
  type NotifyFailure,
} from "@/lib/api";

const message = (failure: unknown) =>
  failure instanceof Error ? failure.message : "Something went wrong.";
const PAIRING_POLL_MS = 3000;
const FLASH_MS = 4000;

function Switch({
  label,
  on,
  disabled,
  onChange,
}: {
  label: string;
  on: boolean;
  disabled?: boolean;
  onChange: (on: boolean) => void;
}) {
  return (
    <button
      aria-checked={on}
      aria-label={label}
      className="schedule-switch"
      disabled={disabled}
      onClick={() => onChange(!on)}
      role="switch"
      type="button"
    />
  );
}

function Failure({ failure }: { failure: NotifyFailure }) {
  return failure ? (
    <p className="form-error" role="alert">
      Last send failed ({new Date(failure.at).toLocaleString()}): {failure.message}
    </p>
  ) : null;
}

/** Telegram in two steps: the bot token, then a code sent to the bot so the app learns the
 * chat itself. The token is write-only; nothing here ever reads it back. */
function TelegramSetup({ onClose, onPaired }: { onClose: () => void; onPaired: () => void }) {
  const [token, setToken] = useState("");
  const [pairing, setPairing] = useState<{ code: string; bot: string } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function check() {
    try {
      if ((await getPairing()).paired) onPaired();
    } catch (failure) {
      setError(message(failure));
    }
  }

  useEffect(() => {
    if (!pairing) return;
    const timer = setInterval(() => void check(), PAIRING_POLL_MS);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pairing]);

  async function start(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      setPairing(await setTelegramToken(token.trim()));
      setToken("");
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal label="Set up Telegram" onClose={onClose}>
      <div className="notify-panel">
        <h3>Set up Telegram</h3>
        {pairing ? (
          <>
            <p>
              Open <strong>@{pairing.bot}</strong> in Telegram and send it this code:
            </p>
            <p className="notify-code">{pairing.code}</p>
            <p>This page notices on its own, or press the button once you have sent it.</p>
            <div className="notify-actions">
              <button onClick={() => void check()} type="button">
                I sent it
              </button>
              <button onClick={onClose} type="button">
                Cancel
              </button>
            </div>
          </>
        ) : (
          <form onSubmit={(event) => void start(event)}>
            <p>
              Create a bot with @BotFather (send it /newbot) and paste the token it gives you. It is
              kept in your keychain and never shown again.
            </p>
            <label className="notify-field">
              Bot token
              <input
                autoComplete="off"
                onChange={(event) => setToken(event.target.value)}
                type="password"
                value={token}
              />
            </label>
            <div className="notify-actions">
              <button data-primary="true" disabled={busy || !token.trim()} type="submit">
                {busy ? "Checking…" : "Continue"}
              </button>
              <button onClick={onClose} type="button">
                Cancel
              </button>
            </div>
          </form>
        )}
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </Modal>
  );
}

// Mail servers people commonly use, guessed from the address so most only type two things.
const SERVERS: Record<string, string> = {
  "gmail.com": "smtp.gmail.com",
  "googlemail.com": "smtp.gmail.com",
  "outlook.com": "smtp-mail.outlook.com",
  "hotmail.com": "smtp-mail.outlook.com",
  "live.com": "smtp-mail.outlook.com",
  "icloud.com": "smtp.mail.me.com",
  "me.com": "smtp.mail.me.com",
  "yahoo.com": "smtp.mail.yahoo.com",
};
const guessServer = (address: string) => SERVERS[address.split("@")[1]?.toLowerCase() ?? ""] ?? "";

/** The digest is sent from your own mailbox, so two things are enough for most people: the
 * address and an app password. Write-only like the token. */
function EmailSetup({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [form, setForm] = useState({ host: "", port: "587", user: "", password: "", to: "" });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const field = (name: keyof typeof form) => ({
    onChange: (event: React.ChangeEvent<HTMLInputElement>) =>
      setForm({ ...form, [name]: event.target.value }),
    value: form[name],
  });
  const host = form.host.trim() || guessServer(form.user.trim());

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!host) {
      setError("We do not know this mail provider. Open “Other mail provider” and enter its server.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      await saveEmail({ ...form, host, port: Number(form.port) });
      onSaved();
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal label="Set up email" onClose={onClose}>
      <form className="notify-panel" onSubmit={(event) => void save(event)}>
        <h3>Set up email</h3>
        <p>
          New matches are emailed from your own mailbox, to yourself. Everything is kept in your
          keychain and never shown again.
        </p>
        <label className="notify-field">
          Your email address
          <input autoComplete="off" placeholder="you@gmail.com" {...field("user")} />
        </label>
        <label className="notify-field">
          App password
          <input autoComplete="new-password" type="password" {...field("password")} />
        </label>
        <p className="notify-help">
          An app password is a separate password that lets this app send mail for you without your
          real one. Create it in your email account&apos;s security settings. For Gmail: Google
          Account, Security, 2-Step Verification, App passwords.
        </p>
        <details className="notify-more">
          <summary>Other mail provider, or send to a different address</summary>
          <label className="notify-field">
            Mail server (SMTP)
            <input autoComplete="off" placeholder={host || "smtp.example.com"} {...field("host")} />
          </label>
          <label className="notify-field">
            Port
            <input inputMode="numeric" {...field("port")} />
          </label>
          <label className="notify-field">
            Send the digest to
            <input autoComplete="off" placeholder="same as your address" {...field("to")} />
          </label>
        </details>
        {error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : null}
        <div className="notify-actions">
          <button data-primary="true" disabled={busy} type="submit">
            {busy ? "Saving…" : "Save"}
          </button>
          <button onClick={onClose} type="button">
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}

/** Pushes new matches to Telegram and email after a run. Off until a channel is set up and
 * switched on; the app works the same without it. Messages leave this machine. */
export function NotificationsSection() {
  const [view, setView] = useState<NotificationsView | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [telegram, setTelegram] = useState(false);
  const [email, setEmail] = useState(false);
  const [setup, setSetup] = useState<"telegram" | "email" | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const show = (next: NotificationsView) => {
    setView(next);
    setEnabled(next.enabled);
    setTelegram(next.telegram.on);
    setEmail(next.email.on);
  };

  useEffect(() => {
    let current = true;
    getNotifications()
      .then((loaded) => current && show(loaded))
      .catch((reason: unknown) => current && setError(message(reason)));
    return () => {
      current = false;
    };
  }, []);

  // "Saved" and the test result are a moment's confirmation, not a state to keep.
  useEffect(() => {
    if (!saved && !note) return;
    const timer = setTimeout(() => {
      setSaved(false);
      setNote("");
    }, FLASH_MS);
    return () => clearTimeout(timer);
  }, [saved, note]);

  const edit = (change: () => void) => {
    setSaved(false);
    setError("");
    setNote("");
    change();
  };

  async function run(action: () => Promise<NotificationsView>) {
    setError("");
    setNote("");
    try {
      show(await action());
    } catch (failure) {
      setError(message(failure));
    }
  }

  async function test(send: () => Promise<{ ok: boolean; error: string | null }>) {
    setError("");
    setNote("");
    try {
      const result = await send();
      if (result.ok) setNote("Test message sent.");
      else setError(result.error ?? "The test failed.");
    } catch (failure) {
      setError(message(failure));
    }
  }

  async function save() {
    setSaving(true);
    setError("");
    try {
      show(await saveNotifications(enabled, telegram, email));
      setSaved(true);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setSaving(false);
    }
  }

  async function finish() {
    setSetup(null);
    await run(getNotifications);
  }

  return (
    <section className="run-card schedule" aria-label="Notifications">
      <Disclosure forceOpen={error !== ""} title="Notifications">
        {view ? (
          <>
            <div className="schedule-head">
              <p className="schedule-sub">
                Get new matches on your phone or in your inbox after a run. Off until you set a
                channel up and switch it on.
              </p>
              <span className="schedule-master">
                Send notifications
                <Switch
                  label="Send notifications"
                  on={enabled}
                  onChange={(on) => edit(() => setEnabled(on))}
                />
              </span>
            </div>
            <div className="slot-grid">
              <div className="slot" data-on={telegram} data-paused={!enabled}>
                <div className="slot-head">
                  <span className="slot-name">Telegram</span>
                  <Switch
                    disabled={!view.telegram.paired}
                    label="Telegram"
                    on={telegram}
                    onChange={(on) => edit(() => setTelegram(on))}
                  />
                </div>
                <p className="notify-state">
                  {view.telegram.paired
                    ? `Paired, ${view.telegram.masked}`
                    : view.telegram.tokenSet
                      ? "Token saved, not paired yet"
                      : "Not set up"}
                </p>
                <div className="notify-actions">
                  <button onClick={() => setSetup("telegram")} type="button">
                    {view.telegram.tokenSet ? "Set up again" : "Set up"}
                  </button>
                  {view.telegram.paired ? (
                    <button onClick={() => void test(testTelegram)} type="button">
                      Send test
                    </button>
                  ) : null}
                  {view.telegram.tokenSet ? (
                    <button onClick={() => void run(removeTelegram)} type="button">
                      Remove
                    </button>
                  ) : null}
                </div>
                <Failure failure={view.telegram.lastFailure} />
              </div>
              <div className="slot" data-on={email} data-paused={!enabled}>
                <div className="slot-head">
                  <span className="slot-name">Email</span>
                  <Switch
                    disabled={!view.email.setUp}
                    label="Email"
                    on={email}
                    onChange={(on) => edit(() => setEmail(on))}
                  />
                </div>
                <p className="notify-state">
                  {view.email.setUp ? `Sends to ${view.email.masked}` : "Not set up"}
                </p>
                <div className="notify-actions">
                  <button onClick={() => setSetup("email")} type="button">
                    {view.email.setUp ? "Set up again" : "Set up"}
                  </button>
                  {view.email.setUp ? (
                    <>
                      <button onClick={() => void test(testEmail)} type="button">
                        Send test
                      </button>
                      <button onClick={() => void run(removeEmail)} type="button">
                        Remove
                      </button>
                    </>
                  ) : null}
                </div>
                <Failure failure={view.email.lastFailure} />
              </div>
            </div>
            <p>
              Up to five new matches per run, with score, reasons and link. Messages leave this
              machine: Telegram and your mail provider keep what is sent. Telegram messages have 👍
              and 👎 buttons that save or dismiss the job.
            </p>
            {!view.keychain ? (
              <p role="status">
                No keychain on this machine: what you enter is kept only until the server stops.
              </p>
            ) : null}
            {error ? (
              <p className="form-error" role="alert">
                {error}
              </p>
            ) : null}
            <div className="schedule-foot">
              <span>{note ? <small role="status">{note}</small> : null}</span>
              <span>
                {saved ? <small role="status">Saved. </small> : null}
                <button
                  className="schedule-save"
                  disabled={saving}
                  onClick={() => void save()}
                  type="button"
                >
                  {saving ? "Saving…" : "Save notifications"}
                </button>
              </span>
            </div>
            {setup === "telegram" ? (
              <TelegramSetup onClose={() => void finish()} onPaired={() => void finish()} />
            ) : null}
            {setup === "email" ? (
              <EmailSetup onClose={() => setSetup(null)} onSaved={() => void finish()} />
            ) : null}
          </>
        ) : error ? (
          <p className="form-error" role="alert">
            {error}
          </p>
        ) : (
          <p>Loading…</p>
        )}
      </Disclosure>
    </section>
  );
}
