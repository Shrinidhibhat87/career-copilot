export type SeriesPoint = { day: string; model: string; tokensPerS: number };

export type ChartBox = { width: number; height: number; pad: number };

export type ChartLine = {
  model: string;
  points: { x: number; y: number; day: string; value: number }[];
};

/** One line per model, positioned in `box`. Days are shared across lines so a model that
 * was idle on a day leaves a gap in its own line, not a shift in everyone else's. */
export function linePaths(series: SeriesPoint[], box: ChartBox): ChartLine[] {
  if (series.length === 0) return [];
  const days = [...new Set(series.map((point) => point.day))].sort();
  const top = Math.max(...series.map((point) => point.tokensPerS));
  const left = box.pad;
  const spanX = box.width - box.pad * 2;
  const bottom = box.height - box.pad;
  const spanY = box.height - box.pad * 2;
  const x = (day: string) =>
    days.length === 1 ? left + spanX / 2 : left + (days.indexOf(day) / (days.length - 1)) * spanX;
  const y = (value: number) => bottom - (top > 0 ? value / top : 0) * spanY;

  const models = [...new Set(series.map((point) => point.model))].sort();
  return models.map((model) => ({
    model,
    points: series
      .filter((point) => point.model === model)
      .sort((a, b) => a.day.localeCompare(b.day))
      .map((point) => ({ x: x(point.day), y: y(point.tokensPerS), day: point.day, value: point.tokensPerS })),
  }));
}
