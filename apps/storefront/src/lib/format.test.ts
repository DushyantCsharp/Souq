/** @jest-environment node */
import { describe, expect, it } from "@jest/globals";
import { categoryLevels, formatPrice, formatPriceParts } from "./format";

describe("formatPriceParts", () => {
  it("isolates the currency symbol and keeps Latin digits in Arabic", () => {
    const parts = formatPriceParts(199, "USD", "ar");
    expect(parts.filter((p) => p.isolate).map((p) => p.text)).toEqual(["US$"]);
    expect(parts.map((p) => p.text).join("")).toMatch(/199\.00/);
  });

  it("falls back to the raw code for an unknown currency", () => {
    expect(formatPriceParts(5, "XX", "en")).toEqual([
      { text: "5", isolate: false },
      { text: " ", isolate: false },
      { text: "XX", isolate: true },
    ]);
    expect(formatPrice(5, "XX", "en")).toBe("5 XX");
  });
});

describe("categoryLevels", () => {
  it("splits on > and drops empty levels", () => {
    expect(categoryLevels("Electronics > Audio >  Headphones > ")).toEqual(["Electronics", "Audio", "Headphones"]);
  });
});
