import type { AgentDecision } from "./types.js";

/**
 * Reaction-gating rules, split from the send path so they stay testable:
 *   - PRIMARY: the model is confident the USER themself would have tapped this
 *     reaction (mirroring their habits) — never rate-limited.
 *   - SECONDARY: the model reacts on its own impulse — rate-limited per chat
 *     (settings.json reactions.cooldown_seconds).
 */
export function reactionAllowed(
  decision: AgentDecision | null,
  lastReactAtMs: number,
  nowMs: number,
  cooldownSeconds: number
): boolean {
  if (!decision?.react) return false;
  if (decision.user_would_react) return true;
  return nowMs - lastReactAtMs >= cooldownSeconds * 1000;
}
