import type { WASocket } from "@whiskeysockets/baileys";
import { settings } from "./config.js";
import { withTimeout } from "./media.js";

const GROUP_META_TIMEOUT_MS = 15_000;

/**
 * Group-participant resolution. Group metadata participants carry BOTH jid
 * forms plus both name flavors:
 *   id/lid — the LID form messages are keyed by
 *   jid    — the phone-number form (…@s.whatsapp.net)
 *   name   — the name the OPERATOR saved in their phone
 *   notify — the name the contact set for themselves (pushName)
 * That bridge is what lets TRUST <number> match LID-keyed senders, and lets
 * the agent see the operator's saved name instead of a nickname.
 */

export interface SenderInfo {
  number: string | null;
  savedName: string | null;
  notifyName: string | null;
}

const cache = new Map<string, { byId: Map<string, SenderInfo>; fetchedAt: number }>();

function cacheTtlMs(): number {
  const minutes = settings.group_metadata?.cache_minutes ?? 10;
  return minutes * 60 * 1000;
}

const digitsOnly = (raw: string | undefined): string | null => {
  const digits = (raw ?? "").replace(/[^0-9]/g, "");
  return digits ? digits : null;
};

export async function senderInfo(
  sock: WASocket,
  groupJid: string,
  participantJid: string
): Promise<SenderInfo | null> {
  const now = Date.now();
  let entry = cache.get(groupJid);
  if (!entry || now - entry.fetchedAt > cacheTtlMs()) {
    try {
      const metadata = await withTimeout(
        sock.groupMetadata(groupJid),
        GROUP_META_TIMEOUT_MS,
        `group metadata ${groupJid}`
      );
      if (!metadata) return null; // timed out: skip this message, retry later
      const byId = new Map<string, SenderInfo>();
      for (const p of metadata.participants ?? []) {
        const info: SenderInfo = {
          number: digitsOnly(p.jid),
          savedName: p.name?.trim() || null,
          notifyName: p.notify?.trim() || null,
        };
        for (const key of [p.id, p.lid]) {
          if (key) byId.set(key.split("@")[0], info);
        }
      }
      entry = { byId, fetchedAt: now };
      cache.set(groupJid, entry);
    } catch (error) {
      console.error(`[group-meta] fetch failed for ${groupJid}:`, error);
      return null;
    }
  }
  const key = participantJid.split("@")[0].split(":")[0];
  return entry.byId.get(key) ?? null;
}

export function clearGroupMetaCacheForTests(): void {
  cache.clear();
}
