/** Euros for a run's ceiling or spend. The cap is a euro or so, so cents are enough; an
 * amount under a cent says so rather than rounding to a misleading €0.00. */
export function euros(value: number): string {
  if (value === 0) return "€0.00";
  if (value < 0.01) return "<€0.01";
  return `€${value.toFixed(2)}`;
}
