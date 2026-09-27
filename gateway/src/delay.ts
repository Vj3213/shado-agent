import type { DelaySettings } from "./config.js";

const randomInt = (min: number, max: number) =>
  min + Math.floor(Math.random() * (max - min + 1));

/**
 * Human-like reply delay, composed of the things humans actually do:
 *   reading the incoming message (scales with ITS length),
 *   typing the reply (scales with reply length),
 *   plus a random 1-5s "life happens" buffer — capped so long messages
 *   never look absurd, floored so instant replies never look robotic.
 */
export function computeReplyDelay(
  incomingText: string,
  replyText: string,
  settings: DelaySettings
): number {
  const [baseMin, baseMax] = settings.base_ms;
  const [readMin, readMax] = settings.reading_ms_per_char;
  const [typeMin, typeMax] = settings.ms_per_char;
  const [extraMin, extraMax] = settings.extra_delay_ms;

  const base = randomInt(baseMin, baseMax);
  const reading = incomingText.length * randomInt(readMin, readMax);
  const typing = replyText.length * randomInt(typeMin, typeMax) / 2;
  const extra = randomInt(extraMin, extraMax);

  const total = base + reading + typing + extra;
  return Math.max(Math.min(total, settings.max_ms), 500);
}

export const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
