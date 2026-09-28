import type { Settings } from "./config.js";
import type { PollSpec } from "./types.js";

/**
 * Poll transport helpers. The AGENT decides whether a poll makes sense and
 * what it says; this module only shapes what Baileys needs and validates the
 * minimum WhatsApp itself requires (a question, at least 2 options).
 */

export interface PollMessage {
  poll: {
    name: string;
    values: string[];
    selectableCount: number;
  };
}

export function buildPollMessage(spec: PollSpec, settings: Settings): PollMessage | null {
  const question = spec.question.trim();
  const options = spec.options.map((o) => o.trim()).filter(Boolean);
  if (!question || options.length < 2) return null;
  return {
    poll: {
      name: question,
      values: options,
      selectableCount: settings.polls?.selectable_count ?? 1,
    },
  };
}

/** The outgoing record the agent stores as context ("You (earlier): ..."). */
export function pollRecordText(spec: PollSpec): string {
  return `[poll] ${spec.question.trim()} (${spec.options.map((o) => o.trim()).join(" / ")})`;
}

export function pollDelayMs(settings: Settings): number {
  const [min, max] = settings.polls?.delay_after_reply_ms ?? [1200, 3500];
  return min + Math.floor(Math.random() * (max - min + 1));
}
