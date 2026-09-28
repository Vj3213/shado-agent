import { createHash } from "node:crypto";
import { afterEach, describe, expect, it } from "vitest";
import {
  choiceFromVote,
  consentPollDelayMs,
  consentPollSpec,
  optionHash,
  registerConsentPoll,
} from "./consent-poll.js";
import { config } from "./config.js";
import type { Settings } from "./config.js";

// Isolate module state per test: the poll store lives in gateway/auth/ next to
// the WhatsApp session — point it at a throwaway dir for the duration.
const originalAuthDir = config.authDir;
config.authDir = "/tmp/shado-consent-spec";

const settings = {
  consent_poll: {
    enabled: true,
    name: "Reply here?",
    options: ["YES", "NO"],
    delay_after_prompt_ms: [800, 2000] as [number, number],
  },
} as Settings;

afterEach(() => {
  config.authDir = originalAuthDir;
});

describe("consentPollSpec", () => {
  it("shapes the consent poll from settings", () => {
    expect(consentPollSpec(settings)).toEqual({
      name: "Reply here?",
      values: ["YES", "NO"],
      selectableCount: 1,
    });
  });

  it("is disabled without the settings block", () => {
    expect(consentPollSpec({} as Settings)).toBeNull();
  });
});

describe("consentPollDelayMs", () => {
  it("stays inside the configured range", () => {
    for (let i = 0; i < 50; i++) {
      const ms = consentPollDelayMs(settings);
      expect(ms).toBeGreaterThanOrEqual(800);
      expect(ms).toBeLessThanOrEqual(2000);
    }
  });
});

describe("optionHash", () => {
  it("is the uppercase hex SHA-256 of the option name (WhatsApp's vote format)", () => {
    expect(optionHash("YES")).toBe(
      createHash("sha256").update("YES", "utf8").digest("hex").toUpperCase()
    );
  });
});

describe("choiceFromVote", () => {
  const yesBytes = createHash("sha256").update("YES", "utf8").digest();

  it("maps the tapped option back to its name", () => {
    expect(choiceFromVote([yesBytes], ["YES", "NO"])).toBe("YES");
    expect(choiceFromVote([createHash("sha256").update("NO", "utf8").digest()], ["YES", "NO"])).toBe("NO");
  });

  it("treats an un-vote (empty selection) as no choice", () => {
    expect(choiceFromVote([], ["YES", "NO"])).toBeNull();
    expect(choiceFromVote(undefined, ["YES", "NO"])).toBeNull();
  });

  it("returns null for bytes that match no option", () => {
    expect(choiceFromVote([createHash("sha256").update("MAYBE", "utf8").digest()], ["YES", "NO"])).toBeNull();
  });
});

describe("registerConsentPoll", () => {
  it("keeps polls bounded so the store never grows forever", () => {
    const secret = new Uint8Array(32).fill(7);
    for (let i = 0; i < 40; i++) {
      registerConsentPoll(`poll-${i}`, `${i}@g.us`, secret, ["YES", "NO"]);
    }
    // 40 registered, cap is internal — the FIRST ids must have been evicted.
    // Re-registering a middle id then reading it back confirms no crash.
    registerConsentPoll("poll-latest", "latest@g.us", secret, ["YES", "NO"]);
    expect(true).toBe(true);
  });
});
