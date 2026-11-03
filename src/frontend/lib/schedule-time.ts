/** Slot times travel as "HH:MM" on the 24-hour clock; the page shows and picks them on 12 hours. */

export type Clock12 = { hour: number; minute: "00" | "30"; pm: boolean };

export function to12(time: string): Clock12 {
  const [h, m] = time.split(":").map(Number);
  return { hour: h % 12 === 0 ? 12 : h % 12, minute: m === 30 ? "30" : "00", pm: h >= 12 };
}

export function to24({ hour, minute, pm }: Clock12): string {
  const h = (hour % 12) + (pm ? 12 : 0);
  return `${String(h).padStart(2, "0")}:${minute}`;
}

/** "18:00" as "06:00 PM". */
export function formatTime(time: string): string {
  const { hour, minute, pm } = to12(time);
  return `${String(hour).padStart(2, "0")}:${minute} ${pm ? "PM" : "AM"}`;
}

/** "Today at 18:00", "Tomorrow at 08:00", else the weekday. */
export function nextLabel(iso: string, now: Date = new Date()): string {
  const at = new Date(iso);
  const days = Math.round(
    (new Date(at.getFullYear(), at.getMonth(), at.getDate()).getTime() -
      new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime()) /
      86_400_000,
  );
  const clock = at.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const day =
    days <= 0 ? "Today" : days === 1 ? "Tomorrow" : at.toLocaleDateString([], { weekday: "long" });
  return `${day} at ${clock}`;
}
