/** Display names for the cities a posting's raw location string mentions.
 *
 * The board gives free text: "München", "Gand", "Berlin, Berlin, Germany",
 * "Amsterdam, Gand, Berlin, Eindhoven". The location filter needs one canonical
 * name per city so those group together. Anything that starts with "Remote" is the
 * single place "Remote", whatever region follows it. */

export const REMOTE = "Remote";

/** Lowercase spelling (diacritics folded) -> the name the filter shows. */
const ALIASES: Record<string, string> = {
  munchen: "Munich",
  muenchen: "Munich",
  koln: "Cologne",
  koeln: "Cologne",
  gand: "Ghent",
  gent: "Ghent",
  wien: "Vienna",
  zurich: "Zurich",
  nurnberg: "Nuremberg",
  nuernberg: "Nuremberg",
  antwerpen: "Antwerp",
  brussel: "Brussels",
  bruxelles: "Brussels",
  "den haag": "The Hague",
  "frankfurt am main": "Frankfurt",
  "frankfurt a m": "Frankfurt",
  "frankfurt am main, germany": "Frankfurt",
};

/** Countries and regions that trail a city and are not cities themselves. */
const NOT_CITIES = new Set([
  "germany",
  "deutschland",
  "belgium",
  "netherlands",
  "the netherlands",
  "france",
  "spain",
  "italy",
  "austria",
  "switzerland",
  "united kingdom",
  "uk",
  "usa",
  "us",
  "united states",
  "eu",
  "emea",
  "europe",
  "de",
]);

const fold = (text: string) =>
  text
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/\s+/g, " ")
    .trim();

const titleCase = (text: string) =>
  text.replace(/\S+/g, (word) => word.charAt(0).toUpperCase() + word.slice(1));

/** The canonical cities a raw location names, in order, without repeats. Empty when
 * the string names none (blank, or only a country). */
export function citiesOf(location: string): string[] {
  const text = location.replace(/\([^)]*\)/g, " ").trim();
  if (!text) return [];
  if (/^remote/i.test(text)) return [REMOTE];

  const found: string[] = [];
  for (const piece of text.split(/[,;|/]|\s[-–—]\s/)) {
    const key = fold(piece);
    if (!key || NOT_CITIES.has(key)) continue;
    const city = ALIASES[key] ?? titleCase(piece.trim());
    if (!found.includes(city)) found.push(city);
  }
  return found;
}
