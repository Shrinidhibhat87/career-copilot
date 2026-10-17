/** A duration a person can read. Local tool calls take microseconds, so whole-centisecond
 * rounding would show them as 0; anything under a second is shown in milliseconds. */
export function formatSeconds(value: number | null): string {
  if (value === null) return "—";
  if (value < 0.001) return "<1 ms";
  if (value < 1) return `${Math.round(value * 1000)} ms`;
  return `${Number(value.toFixed(1))} s`;
}

/** Lower edges of the slow-step levels, in seconds: 3-5 s is level 1, 5-8 s level 2, 8-10 s
 * level 3 and 10 s or more level 4. Under 3 s is level 0, which is not highlighted. */
export const SLOW_STEP_EDGES = [3, 5, 8, 10] as const;

export function slowLevel(value: number | null): 0 | 1 | 2 | 3 | 4 {
  if (value === null) return 0;
  return SLOW_STEP_EDGES.filter((edge) => value >= edge).length as 0 | 1 | 2 | 3 | 4;
}
