import type { DelaySettings } from "./config.js";

const randomInt = (min: number, max: number) =>
  min + Math.floor(Math.random() * (max - min + 1));

/**
 * Human-like reply delay: a randomized base plus time proportional to the
 * length of what we're about to "type", with jitter, capped so long replies
 * never look absurd. Never returns less than 500ms — instant replies look robotic.
 */
export function computeReplyDelay(text: string, settings: DelaySettings): number {
  const [baseMin, baseMax] = settings.base_ms;
  const [perCharMin, perCharMax] = settings.ms_per_char;
  const base = randomInt(baseMin, baseMax);
  const typing = text.length * randomInt(perCharMin, perCharMax) / 2;
  const total = Math.min(base + typing, settings.max_ms);
  return Math.max(total, 500);
}

export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
