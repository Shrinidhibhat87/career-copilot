"use client";

import { useEffect, useState } from "react";

import { Disclosure } from "@/components/disclosure";
import { TimeWheel } from "@/components/time-wheel";
import { getSchedule, saveSchedule, type ScheduleSlot, type ScheduleView } from "@/lib/api";
import { formatTime, nextLabel } from "@/lib/schedule-time";

const NAMES = ["Morning", "Midday", "Evening", "Night"];
const message = (failure: unknown) =>
  failure instanceof Error ? failure.message : "Something went wrong.";
const when = (iso: string) => new Date(iso).toLocaleString();

function Icon({ night }: { night: boolean }) {
  return (
    <svg aria-hidden="true" fill="none" height="18" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24" width="18">
      {night ? (
        <path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z" />
      ) : (
        <>
          <circle cx="12" cy="12" r="4" />
          <path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1 7 17M17 7l2.1-2.1" />
        </>
      )}
    </svg>
  );
}

function Switch({ label, on, onChange }: { label: string; on: boolean; onChange: (on: boolean) => void }) {
  return (
    <button
      aria-checked={on}
      aria-label={label}
      className="schedule-switch"
      onClick={() => onChange(!on)}
      role="switch"
      type="button"
    />
  );
}

/** Four daily run slots, each on or off with its own time (hour or half hour). A run starts
 * only while the app is open and signed in, and only on local models; a time that passed
 * while the app was closed is made up once when the app is back. */
export function ScheduleSection() {
  const [view, setView] = useState<ScheduleView | null>(null);
  const [enabled, setEnabled] = useState(false);
  const [slots, setSlots] = useState<ScheduleSlot[]>([]);
  const [picking, setPicking] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  const show = (next: ScheduleView) => {
    setView(next);
    setEnabled(next.enabled);
    setSlots(next.slots);
  };

  useEffect(() => {
    let current = true;
    getSchedule()
      .then((loaded) => current && show(loaded))
      .catch((reason: unknown) => current && setError(message(reason)));
    return () => {
      current = false;
    };
  }, []);

  // "Saved" is a moment's confirmation, not a state to keep.
  useEffect(() => {
    if (!saved) return;
    const timer = setTimeout(() => setSaved(false), 4000);
    return () => clearTimeout(timer);
  }, [saved]);

  const edit = (change: () => void) => {
    setSaved(false);
    setError("");
    change();
  };
  const setSlot = (index: number, change: Partial<ScheduleSlot>) =>
    edit(() => setSlots(slots.map((slot, i) => (i === index ? { ...slot, ...change } : slot))));

  const onTimes = slots.filter((slot) => slot.on).map((slot) => slot.time);
  const duplicate = new Set(onTimes).size !== onTimes.length;

  async function save() {
    setSaving(true);
    setError("");
    try {
      show(await saveSchedule(enabled, slots));
      setSaved(true);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="run-card schedule" aria-label="Schedule">
      <Disclosure forceOpen={error !== ""} title="Schedule">
        {view ? (
          <>
            <div className="schedule-head">
              <p className="schedule-sub">Four slots a day. Switch on the ones you want and set their times.</p>
              <span className="schedule-master">
                Run daily
                <Switch label="Run daily" on={enabled} onChange={(on) => edit(() => setEnabled(on))} />
              </span>
            </div>
            <div className="slot-grid">
              {slots.map((slot, index) => (
                <div className="slot" data-on={slot.on} data-paused={!enabled} key={NAMES[index]}>
                  <div className="slot-head">
                    <span className="slot-name">
                      <Icon night={index === 3} />
                      {NAMES[index]}
                    </span>
                    <Switch
                      label={`${NAMES[index]} slot`}
                      on={slot.on}
                      onChange={(on) => setSlot(index, { on })}
                    />
                  </div>
                  <button
                    aria-label={`${NAMES[index]} time`}
                    className="slot-time"
                    onClick={() => setPicking(index)}
                    type="button"
                  >
                    {formatTime(slot.time)}
                  </button>
                </div>
              ))}
            </div>
            <p className="schedule-label">If the app is closed at run time</p>
            <span className="schedule-pill">Run when I open it</span>
            <p>
              If you open the app after a missed time, it runs once straight away. Several missed
              times still count as one run.
            </p>
            {duplicate ? (
              <p className="form-error" role="alert">
                Two slots that are on have the same time.
              </p>
            ) : null}
            {error ? (
              <p className="form-error" role="alert">
                {error}
              </p>
            ) : null}
            {view.enabled && !view.canRun ? (
              <p role="status">A scheduled run would be skipped now: {view.whyNot}</p>
            ) : null}
            {view.lastSkip ? (
              <p>
                Last skip ({when(view.lastSkip.at)}): {view.lastSkip.reason}
              </p>
            ) : null}
            {view.lastRunAt ? <p>Last scheduled run: {when(view.lastRunAt)}.</p> : null}
            <div className="schedule-foot">
              <div className="schedule-next">
                <div>
                  <small>Next run</small>
                  <strong>{view.enabled && view.next ? nextLabel(view.next) : "Off"}</strong>
                </div>
              </div>
              <span>
                {saved ? <small role="status">Saved. </small> : null}
                <button
                  className="schedule-save"
                  disabled={saving || duplicate}
                  onClick={() => void save()}
                  type="button"
                >
                  {saving ? "Saving…" : "Save schedule"}
                </button>
              </span>
            </div>
            {picking !== null ? (
              <TimeWheel
                onCancel={() => setPicking(null)}
                onDone={(time) => {
                  setSlot(picking, { time });
                  setPicking(null);
                }}
                title={`${NAMES[picking]} time`}
                value={slots[picking].time}
              />
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
