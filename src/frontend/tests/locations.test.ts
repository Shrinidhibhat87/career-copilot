import { describe, expect, it } from "vitest";

import { citiesOf } from "@/lib/locations";

describe("citiesOf", () => {
  it("maps local spellings to one display name", () => {
    expect(citiesOf("München")).toEqual(["Munich"]);
    expect(citiesOf("Gand")).toEqual(["Ghent"]);
    expect(citiesOf("Köln")).toEqual(["Cologne"]);
  });

  it("drops the country and repeats from a city string", () => {
    expect(citiesOf("Berlin, Berlin, Germany")).toEqual(["Berlin"]);
  });

  it("lists every city of a multi-city posting", () => {
    expect(citiesOf("Amsterdam, Gand, Berlin, Eindhoven")).toEqual([
      "Amsterdam",
      "Ghent",
      "Berlin",
      "Eindhoven",
    ]);
  });

  it("calls anything that starts with Remote just Remote", () => {
    expect(citiesOf("Remote US, West Coast")).toEqual(["Remote"]);
    expect(citiesOf("remote - Germany")).toEqual(["Remote"]);
  });

  it("names nothing for a blank or country-only location", () => {
    expect(citiesOf("")).toEqual([]);
    expect(citiesOf("Germany")).toEqual([]);
  });
});
