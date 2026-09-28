import { describe, expect, it } from "vitest";
import { buildPollMessage, pollDelayMs, pollRecordText } from "./poll.js";
import type { Settings } from "./config.js";
import type { PollSpec } from "./types.js";

const settings = {
  polls: {
    selectable_count: 1,
    delay_after_reply_ms: [1200, 3500] as [number, number],
  },
} as Settings;

const spec: PollSpec = {
  question: "Aaj kya banau?",
  options: ["poha", "upma", "Maggi"],
};

describe("buildPollMessage", () => {
  it("shapes the Baileys poll payload", () => {
    expect(buildPollMessage(spec, settings)).toEqual({
      poll: { name: "Aaj kya banau?", values: ["poha", "upma", "Maggi"], selectableCount: 1 },
    });
  });

  it("rejects polls WhatsApp cannot render", () => {
    expect(buildPollMessage({ question: "  ", options: spec.options }, settings)).toBeNull();
    expect(buildPollMessage({ question: spec.question, options: ["only one"] }, settings)).toBeNull();
    expect(buildPollMessage({ question: spec.question, options: ["", "   "] }, settings)).toBeNull();
  });

  it("works without a polls settings block (legacy settings.json)", () => {
    expect(buildPollMessage(spec, {} as Settings)).toEqual({
      poll: { name: "Aaj kya banau?", values: spec.options, selectableCount: 1 },
    });
  });
});

describe("pollRecordText", () => {
  it("formats the context record the agent will see in history", () => {
    expect(pollRecordText(spec)).toBe("[poll] Aaj kya banau? (poha / upma / Maggi)");
  });
});

describe("pollDelayMs", () => {
  it("stays inside the configured range", () => {
    for (let i = 0; i < 50; i++) {
      const ms = pollDelayMs(settings);
      expect(ms).toBeGreaterThanOrEqual(1200);
      expect(ms).toBeLessThanOrEqual(3500);
    }
  });
});
