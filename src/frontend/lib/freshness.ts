/** How old a posting is, and when that age makes its link untrustworthy.
 *
 * A board takes a filled role down the moment it is filled; the crawler only retires a
 * posting after `inactive_after_days` of not seeing it. In that window the row is still
 * listed and its link lands on the board's own "no longer available" page — which is what
 * `?error=true` on a Greenhouse board root is. The screen has to warn before the click.
 */

export function daysSince(iso: string, now: number = Date.now()) {
  return Math.floor((now - new Date(iso).getTime()) / 86_400_000);
}

/** Compact age for a meta line: `today`, else `12d`. */
export function age(iso: string, now: number = Date.now()) {
  const days = daysSince(iso, now);
  return days <= 0 ? "today" : `${days}d`;
}

/** Half the crawler's own horizon, so the threshold moves with `inactive_after_days`
 * rather than being a second number this screen invents. */
export function mayBeClosed(iso: string, inactiveAfterDays: number, now: number = Date.now()) {
  return daysSince(iso, now) >= Math.ceil(inactiveAfterDays / 2);
}
