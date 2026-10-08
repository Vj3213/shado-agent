import dotenv from "dotenv";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url)); // gateway/src or gateway/dist
const root = path.join(here, "../..");

dotenv.config({ path: path.join(root, ".env") });

export interface DelaySettings {
  base_ms: [number, number];
  reading_ms_per_char: [number, number];
  ms_per_char: [number, number];
  extra_delay_ms: [number, number];
  max_ms: number;
}

export interface Settings {
  allow_private_chats: boolean;
  delays: DelaySettings;
  reactions: { allowed: string[]; cooldown_seconds: number };
  polls?: {
    selectable_count: number;
    delay_after_reply_ms: [number, number];
  };
  consent_poll?: {
    enabled: boolean;
    name: string;
    options: string[];
    delay_after_prompt_ms: [number, number];
  };
  group_metadata?: {
    cache_minutes: number;
  };
  media?: {
    reply_to_media: boolean;
    max_mb: number;
    download_timeout_seconds: number;
    allowed_mimetypes: string[];
  };
  read_receipts?: {
    enabled: boolean;
  };
}

export const settings: Settings = JSON.parse(
  readFileSync(path.join(root, "config", "settings.json"), "utf8")
) as Settings;

function required(name: string): string {
  const value = process.env[name];
  if (!value || value === "TODO") {
    throw new Error(`Missing required env var: ${name}`);
  }
  return value;
}

export const config = {
  groupJid: process.env.GROUP_JID && process.env.GROUP_JID !== "TODO" ? process.env.GROUP_JID : undefined,
  agentUrl: process.env.AGENT_URL ?? "http://127.0.0.1:8100",
  gatewayPort: Number(process.env.GATEWAY_PORT ?? 8090),
  linkPhoneNumber: process.env.LINK_PHONE_NUMBER || undefined,
  operatorJid: process.env.OPERATOR_JID || undefined,
  /** Plain operator number (no +, no @) — votes encrypt against the voter's
   * PN format; the chat JID we see is often the LID, which won't decrypt. */
  operatorPhone: process.env.OPERATOR_PHONE || undefined,
  authDir: path.join(root, "gateway", "auth"),
};

/** Allowlist: the configured group, plus private chats when enabled. */
export function chatAllowed(jid: string): boolean {
  if (jid === config.groupJid) return true;
  if (settings.allow_private_chats && !jid.endsWith("@g.us")) return true;
  return false;
}

/**
 * Discovery mode: with no GROUP_JID configured, log the JID of every group we
 * see (once each) so the operator can pick one for GROUP_JID. Nothing is
 * forwarded to the agent in this mode.
 */
export const discoveryMode = config.groupJid === undefined;
