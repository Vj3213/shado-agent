import { describe, expect, it } from "vitest";
import { reactionAllowed } from "./reaction.js";
import type { AgentDecision } from "./types.js";

const base: AgentDecision = { reply: "ok", reason: "ok" };
const withReact = (userWouldReact?: boolean): AgentDecision => ({
  ...base,
  react: "👍",
  ...(userWouldReact === undefined ? {} : { user_would_react: userWouldReact }),
});

describe("reactionAllowed", () => {
  it("no reaction emoji -> never", () => {
    expect(reactionAllowed(base, 0, 999_999, 120)).toBe(false);
    expect(reactionAllowed(null, 0, 999_999, 120)).toBe(false);
  });

  it("PRIMARY: mirroring the user's hand is never rate-limited", () => {
    // Even a reaction sent 1ms ago does not block a user-mirrored one.
    expect(reactionAllowed(withReact(true), 999_999, 1_000_000, 120)).toBe(true);
  });

  it("SECONDARY: the model's own impulse respects the per-chat cooldown", () => {
    const now = 1_000_000;
    expect(reactionAllowed(withReact(false), now - 119_000, now, 120)).toBe(false);
    expect(reactionAllowed(withReact(false), now - 120_000, now, 120)).toBe(true);
  });

  it("absent flag defaults to the rate-limited path (backwards compatible)", () => {
    const now = 1_000_000;
    expect(reactionAllowed(withReact(undefined), now - 10_000, now, 120)).toBe(false);
  });
});
