import type { proto } from "@whiskeysockets/baileys";
import { jidNormalizedUser } from "@whiskeysockets/baileys";
import { chatAllowed, config, discoveryMode, settings } from "./config.js";
import { askAgent, askAgentConsole, reportOutgoing } from "./agent-client.js";
import { computeReplyDelay, sleep } from "./delay.js";
import type { IncomingMessage, OutgoingSource } from "./types.js";
import type { WASocket } from "@whiskeysockets/baileys";

type WAMessage = proto.IWebMessageInfo;

/** The bot account's own JID, properly normalized (device part stripped). */
const selfJidOf = (sock: WASocket): string =>
  sock.user?.id ? jidNormalizedUser(sock.user.id) : "";

const seenChats = new Set<string>();

/** Ids of messages this process itself sent (already recorded via reportOutgoing). */
const ourSentMessageIds = new Set<string>();

/** Per-chat reaction cooldown — humans react a few times a day, not per message. */
const lastReactAt = new Map<string, number>();

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

      const selfJid = selfJidOf(sock);
      if (selfJid && jidNormalizedUser(jid) === selfJid) {
        // Operator console: commands typed in the bot phone's "Message yourself" chat.
        const cmd = extractText(msg);
        if (cmd && msg.messageTimestamp && !discoveryMode) {
          console.log(`[console] ${cmd}`);
          const d = await askAgentConsole(cmd);
          if (d?.reply) await sendHumanLike(sock, jid, d.reply);
        }
        continue;
      }

      // A manual opener typed on the bot's own phone: record it for context,
      // and if it went to a brand-new chat, surface the consent prompt.
      if (!discoveryMode && !jid.endsWith("@broadcast") && !jid.endsWith("@newsletter")) {
        const manualText = extractText(msg);
        if (manualText && msg.messageTimestamp) {
          console.log(`[recv] (manual from our phone) ${manualText.slice(0, 60)}`);
          const d = await reportOutgoing({
            group_id: jid,
            text: manualText,
            timestamp: new Date(Number(msg.messageTimestamp) * 1000).toISOString(),
            source: "operator",
          });
          const selfJid2 = selfJidOf(sock);
          if (d?.self_prompt && selfJid2) await sendHumanLike(sock, selfJid2, d.self_prompt);
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

    // WhatsApp system channels never reach the agent.
    if (jid.endsWith("@broadcast") || jid.endsWith("@newsletter")) continue;

    // Everything else is forwarded: the agent owns access decisions
    // (static allowlist + consent-gating for unknown chats).

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
    if (decision?.self_prompt) {
      // Consent prompts go to the operator console: their DM if configured
      // (reliable transport), else the self-chat (may not decrypt on phones).
      const consoleTarget = config.operatorJid
        ? jidNormalizedUser(config.operatorJid)
        : selfJidOf(sock);
      if (consoleTarget) await sendHumanLike(sock, consoleTarget, decision.self_prompt);
    }
    // Instant tap-reaction BEFORE the typed reply — that's the human order:
    // tap the emoji, then take your time writing. Rate-limited per chat so
    // even a misbehaving model can't turn reactions into a habit.
    const nowMs = Date.now();
    const lastReact = lastReactAt.get(jid) ?? 0;
    if (decision?.react && key.id && nowMs - lastReact >= settings.reactions.cooldown_seconds * 1000) {
      try {
        lastReactAt.set(jid, nowMs);
        await sleep(300 + Math.random() * 600);
        await sock.sendMessage(jid, {
          react: { text: decision.react, key: { remoteJid: jid, fromMe: false, id: key.id, participant: incoming.sender } },
        });
        console.log(`[react] ${decision.react} on msg ${key.id.slice(0, 10)}`);
      } catch (reactError) {
        console.error("[react] failed:", reactError);
      }
    } else if (decision?.react) {
      console.log("[react] skipped — cooldown active for this chat");
    }
    if (!decision?.reply) {
      if (decision) console.log(`[agent] no reply (${decision.reason})`);
      continue;
    }

    await sendHumanLike(sock, jid, decision.reply, text);
  }
}

/**
 * Core send path with human-like behavior: randomized delay scaled to reply
 * length, WhatsApp "composing…" presence, then exactly one message.
 */
export async function sendHumanLike(
  sock: WASocket,
  groupJid: string,
  text: string,
  incomingText = "",
  source: OutgoingSource = "agent"
): Promise<void> {
  const delayMs = computeReplyDelay(incomingText, text, settings.delays);
  console.log(`[send] waiting ${delayMs}ms before replying`);
  await sleep(delayMs);

  try {
    // Presence is cosmetic — never let it block (or crash) the actual send.
    try {
      await sock.sendPresenceUpdate("composing", groupJid);
      await sleep(800 + Math.random() * 900); // composing beats before send
    } catch (presenceError) {
      console.error("[send] presence update failed (continuing):", presenceError);
    }
    const sent = await sock.sendMessage(groupJid, { text });
    try {
      await sock.sendPresenceUpdate("paused", groupJid);
    } catch {
      /* ignore */
    }
    if (sent?.key?.id) ourSentMessageIds.add(sent.key.id);
  } catch (error) {
    console.error("[send] failed to send message:", error);
    return;
  }

  const timestamp = new Date().toISOString();
  console.log(`[sent] ${text.slice(0, 80)}`);
  // Self-chat traffic (operator prompts/replies) isn't conversation context.
  if (selfJidOf(sock) && jidNormalizedUser(groupJid) === selfJidOf(sock)) return;
  void reportOutgoing({ group_id: groupJid, text, timestamp, source });
}
