import { afterEach, describe, expect, it, vi } from "vitest";
import { computeReplyDelay } from "./delay.js";
import type { DelaySettings } from "./config.js";

afterEach(() => {
  vi.restoreAllMocks();
});

// Pin Math.random so the delay math is exact: randomInt(min, max) then
// always returns min (seeded 0) or max (seeded ~1).
function seedRandom(value: number) {
  vi.spyOn(Math, "random").mockReturnValue(value);
}

const settings: DelaySettings = {
  base_ms: [1000, 2000],
  reading_ms_per_char: [1, 2],
  ms_per_char: [10, 20],
  extra_delay_ms: [0, 0],
  max_ms: 5000,
};

describe("computeReplyDelay", () => {
  it("composes base + reading(incoming) + typing(reply) at the minimum", () => {
    seedRandom(0);
    // 1000 base + 5 chars * 1ms reading + 2 chars * 10ms / 2 typing
    expect(computeReplyDelay("hello", "hi", settings)).toBe(1015);
  });

  it("composes at the maximum of every range", () => {
    seedRandom(0.999999);
    // 2000 base + 5 * 2ms reading + 2 * 20ms / 2 typing
    expect(computeReplyDelay("hello", "hi", settings)).toBe(2030);
  });

  it("reading time tracks the INCOMING length, typing the reply", () => {
    seedRandom(0);
    expect(computeReplyDelay("a".repeat(10), "", settings)).toBe(1010);
    expect(computeReplyDelay("", "b".repeat(10), settings)).toBe(1050);
  });

  it("never exceeds max_ms, however long the texts", () => {
    seedRandom(0);
    expect(computeReplyDelay("x".repeat(10000), "y".repeat(1000), settings)).toBe(5000);
  });

  it("never goes below 500ms, however short the texts", () => {
    seedRandom(0);
    const zero = {
      base_ms: [0, 0],
      reading_ms_per_char: [0, 0],
      ms_per_char: [0, 0],
      extra_delay_ms: [0, 0],
      max_ms: 20000,
    } satisfies DelaySettings;
    expect(computeReplyDelay("", "", zero)).toBe(500);
  });
});
