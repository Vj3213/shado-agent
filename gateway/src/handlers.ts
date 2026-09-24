import type { proto } from "@whiskeysockets/baileys";
import { chatAllowed, config, discoveryMode, settings } from "./config.js";
import { askAgent, reportOutgoing } from "./agent-client.js";
import { computeReplyDelay, sleep } from "./delay.js";
import type { IncomingMessage } from "./types.js";
import type { WASocket } from "@whiskeysockets/baileys";

type WAMessage = proto.IWebMessageInfo;

const seenChats = new Set<string>();

/** Ids of messages this process itself sent (already recorded via reportOutgoing). */
const ourSentMessageIds = new Set<string>();

/** Best-effort text extraction from the various WhatsApp message shapes. */
export function extractText(msg: WAMessage): string | null {
  return (
    msg.message?.conversation ??
    msg.message?.extendedTextMessage?.text ??
    msg.message?.imageMessage?.caption ??
    msg.message?.videoMessage?.caption ??
    null
  );
}

/**
 * Core receive path. Rules, in order:
 *   1. Only notify-style, text-bearing messages.
 *   2. Only the allowlisted group; never our own outgoing messages.
 *   3. Forward to the agent; the agent owns every "should I reply?" decision.
 */
export async function handleMessagesUpsert(sock: WASocket, upsert: { messages: WAMessage[]; type: string }): Promise<void> {
  if (upsert.type !== "notify") return;

  for (const msg of upsert.messages) {
    const key = msg.key;
    if (!key.remoteJid) continue;

    const jid = key.remoteJid;
    const isGroup = jid.endsWith("@g.us");

    if (key.fromMe) {
      // Our own relay sends are already recorded — skip them.
      if (key.id && ourSentMessageIds.has(key.id)) continue;
      // A manual opener typed on the bot's own phone: record it for context
      // (the model should know this conversation started), but never reply to it.
      if (!discoveryMode && chatAllowed(jid)) {
        const manualText = extractText(msg);
        if (manualText && msg.messageTimestamp) {
          console.log(`[recv] (manual from our phone) ${manualText.slice(0, 60)}`);
          void reportOutgoing({
            group_id: jid,
            text: manualText,
            timestamp: new Date(Number(msg.messageTimestamp) * 1000).toISOString(),
          });
        }
      }
      continue;
    }

    if (discoveryMode) {
      // Discovery: log one line per chat/group seen; forward nothing.
      if (!seenChats.has(jid)) {
        seenChats.add(jid);
        console.log(`[discovery] ${isGroup ? "group" : "chat"} seen: ${jid}`);
      }
      continue;
    }

    if (!chatAllowed(jid)) continue;

    const text = extractText(msg);
    if (!text || !msg.messageTimestamp) continue;

    const incoming: IncomingMessage = {
      group_id: jid,
      sender: isGroup ? (key.participant ?? jid) : jid,
      text,
      timestamp: new Date(Number(msg.messageTimestamp) * 1000).toISOString(),
    };

    console.log(`[recv] ${incoming.sender}: ${text.slice(0, 80)}`);
    const decision = await askAgent(incoming);
    if (!decision?.reply) {
      if (decision) console.log(`[agent] no reply (${decision.reason})`);
      continue;
    }

    await sendHumanLike(sock, jid, decision.reply);
  }
}

/**
 * Core send path with human-like behavior: randomized delay scaled to reply
 * length, WhatsApp "composing…" presence, then exactly one message.
 */
export async function sendHumanLike(sock: WASocket, groupJid: string, text: string): Promise<void> {
  const delayMs = computeReplyDelay(text, settings.delays);
  console.log(`[send] waiting ${delayMs}ms before replying`);
  await sleep(delayMs);

  try {
    await sock.sendPresenceUpdate("composing", groupJid);
    await sleep(800 + Math.random() * 900); // composing beats before send
    const sent = await sock.sendMessage(groupJid, { text });
    await sock.sendPresenceUpdate("paused", groupJid);
    if (sent?.key?.id) ourSentMessageIds.add(sent.key.id);
  } catch (error) {
    console.error("[send] failed to send message:", error);
    return;
  }

  const timestamp = new Date().toISOString();
  console.log(`[sent] ${text.slice(0, 80)}`);
  void reportOutgoing({ group_id: groupJid, text, timestamp });
}
