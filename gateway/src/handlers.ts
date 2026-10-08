import type { proto, WAMessageKey } from "@whiskeysockets/baileys";
import { jidNormalizedUser } from "@whiskeysockets/baileys";
import { chatAllowed, config, discoveryMode, settings } from "./config.js";
import { askAgent, askAgentConsole, askAgentMedia, reportOperatorReaction, reportOutgoing } from "./agent-client.js";
import { computeReplyDelay, sleep } from "./delay.js";
import { detectMedia, resolveMediaBytes } from "./media.js";
import { buildPollMessage, pollDelayMs, pollRecordText } from "./poll.js";
import { reactionAllowed } from "./reaction.js";
import { senderInfo } from "./group-meta.js";
import {
  consentPollDelayMs,
  consentPollSpec,
  parseConsentVote,
  registerConsentPoll,
} from "./consent-poll.js";
import { randomBytes } from "node:crypto";
import type { AgentDecision, IncomingMessage, OutgoingSource } from "./types.js";
import type { WASocket } from "@whiskeysockets/baileys";

type WAMessage = proto.IWebMessageInfo;

/** The bot account's own JID, properly normalized (device part stripped). */
const selfJidOf = (sock: WASocket): string =>
  sock.user?.id ? jidNormalizedUser(sock.user.id) : "";

/**
 * The self-chat ("Message yourself") is the operator console. WhatsApp may
 * key it in either the PN form (…@s.whatsapp.net) or the LID form (…@lid) —
 * compare against both, else console commands silently become plain messages.
 */
export function isSelfChat(jid: string, sock: WASocket): boolean {
  const normalized = jidNormalizedUser(jid);
  const pn = sock.user?.id ? jidNormalizedUser(sock.user.id) : "";
  const lid = sock.user?.lid ? jidNormalizedUser(sock.user.lid) : "";
  return (pn !== "" && normalized === pn) || (lid !== "" && normalized === lid);
}

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
    try {
      await processOneMessage(sock, upsert, msg);
    } catch (error) {
      console.error("[recv] message processing failed (skipped):", error);
    }
  }
}

async function processOneMessage(
  sock: WASocket,
  upsert: { messages: proto.IWebMessageInfo[]; type: string },
  msg: proto.IWebMessageInfo
): Promise<void> {
    const key = msg.key;
    if (!key.remoteJid) return;

    // Consent-poll votes arrive as encrypted pollUpdateMessage entries —
    // resolve them to a console command before any other handling.
    if (msg.message?.pollUpdateMessage) {
      await handleConsentVote(sock, msg);
      return;
    }

    const jid = key.remoteJid;
    const isGroup = jid.endsWith("@g.us");

    if (key.fromMe) {
      // Our own relay sends are already recorded — skip them.
      if (key.id && ourSentMessageIds.has(key.id)) return;

      // Operator's manual reactions: teach Shado new emojis — in the chat
      // they were made, so it can mirror the operator's per-chat habits.
      const operatorReact = msg.message?.reactionMessage?.text;
      if (operatorReact && !discoveryMode) {
        console.log(`[react-learn] operator reacted with ${operatorReact} in ${jid}`);
        void reportOperatorReaction(operatorReact, jid);
        return;
      }

      if (isSelfChat(jid, sock)) {
        // Operator console: commands typed in the bot phone's "Message yourself" chat.
        const cmd = extractText(msg);
        if (cmd && msg.messageTimestamp && !discoveryMode) {
          console.log(`[console] ${cmd}`);
          const d = await askAgentConsole(cmd);
          if (d?.reply) await sendHumanLike(sock, jid, d.reply);
          await deliverIfAny(sock, d);
        }
        return;
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
          // Commands typed into the operator DM from THIS phone are console
          // commands too (agent runs them) — answer right here.
          if (d?.reply) {
            await sendHumanLike(sock, jid, d.reply);
            await deliverIfAny(sock, d);
          }
          const selfJid2 = selfJidOf(sock);
          if (d?.self_prompt && selfJid2) {
            await sendHumanLike(sock, selfJid2, d.self_prompt);
            await sendConsentPoll(sock, selfJid2, jid);
          }
        }
      }
      return;
    }

    if (discoveryMode) {
      // Discovery: log one line per chat/group seen; forward nothing.
      if (!seenChats.has(jid)) {
        seenChats.add(jid);
        console.log(`[discovery] ${isGroup ? "group" : "chat"} seen: ${jid}`);
      }
      return;
    }

    // WhatsApp system channels never reach the agent.
    if (jid.endsWith("@broadcast") || jid.endsWith("@newsletter")) return;

    // Reactions the OPERATOR taps from their personal phone (in their DM with
    // the bot) are operator vocabulary too — group reactions by others are not.
    const incomingReact = msg.message?.reactionMessage?.text;
    if (incomingReact && config.operatorJid && jidNormalizedUser(jid) === jidNormalizedUser(config.operatorJid)) {
      console.log(`[react-learn] operator reacted ${incomingReact} (their phone)`);
      void reportOperatorReaction(incomingReact, jid);
      return;
    }

    // Everything else is forwarded: the agent owns access decisions
    // (static allowlist + consent-gating for unknown chats).

    if (!msg.messageTimestamp) return;

    // Media first: a caption-less image/sticker/GIF must still reach the agent.
    const detected = detectMedia(msg);
    const media = detected ? await resolveMediaBytes(msg, sock, detected) : null;

    const text = extractText(msg);
    if (!text && !media) return;

    // Group members resolve through metadata: the operator's SAVED name and
    // the sender's real phone number (the PN↔LID bridge for number trust).
    let senderName: string | null = msg.pushName || null;
    let senderNumber: string | null = null;
    if (isGroup) {
      const info = await senderInfo(sock, jid, (key.participant ?? jid));
      if (info) {
        senderName = info.savedName ?? info.notifyName ?? senderName;
        senderNumber = info.number;
      }
    }

    const incoming: IncomingMessage = {
      group_id: jid,
      sender: isGroup ? (key.participant ?? jid) : jid,
      text: text ?? `[${media!.kind}]`,
      timestamp: new Date(Number(msg.messageTimestamp) * 1000).toISOString(),
      sender_name: senderName,
      sender_number: senderNumber,
    };

    if (media) {
      console.log(`[recv-media] ${incoming.sender}: ${media.kind}${media.caption ? " +caption" : ""}`);
      const decision = await askAgentMedia({
        group_id: incoming.group_id,
        sender: incoming.sender,
        sender_name: senderName,
        sender_number: senderNumber,
        caption: media.caption,
        kind: media.kind,
        mimetype: media.mimetype,
        media_base64: media.base64,
        timestamp: incoming.timestamp,
      });
      await actOnDecision(sock, jid, key, incoming, decision, text ?? "");
      return;
    }

    console.log(`[recv] ${incoming.sender}: ${text!.slice(0, 80)}`);
    const decision = await askAgent(incoming);
    await actOnDecision(sock, jid, key, incoming, decision, text!);
}

/** Shared reaction/reply/poll/deliver handling for BOTH the text and media
 * paths — one code path, one set of safety rails. */
async function actOnDecision(
  sock: WASocket,
  jid: string,
  key: proto.IMessageKey,
  incoming: IncomingMessage,
  decision: AgentDecision | null,
  incomingText: string
): Promise<void> {
  if (decision?.self_prompt) {
    // Consent prompts go to the operator console: their DM if configured
    // (reliable transport), else the self-chat (may not decrypt on phones).
    const consoleTarget = config.operatorJid
      ? jidNormalizedUser(config.operatorJid)
      : selfJidOf(sock);
    if (consoleTarget) {
      await sendHumanLike(sock, consoleTarget, decision.self_prompt);
      await sendConsentPoll(sock, consoleTarget, jid);
    }
  }
  // Blue tick the moment we ENGAGE (reply or react) — that's the human order:
  // read it, then act. Ignored messages stay unticked (humans don't read
  // every group message instantly either). Needs "Read receipts" ON in the
  // phone's WhatsApp privacy settings to reach others as blue ticks.
  if (
    settings.read_receipts?.enabled !== false &&
    key.id &&
    key.remoteJid &&
    decision &&
    (decision.reply || decision.react)
  ) {
    try {
      await sock.readMessages([
        { ...key, remoteJid: key.remoteJid, fromMe: false } as WAMessageKey,
      ]);
      console.log(`[read] marked read (msg ${key.id.slice(0, 10)})`);
    } catch (readError) {
      console.error("[read] receipt failed:", readError);
    }
  }
  // Instant tap-reaction BEFORE the typed reply — that's the human order:
  // tap the emoji, then take your time writing. Rate-limited per chat ONLY
  // for the model's own impulses — reactions mirroring the user's hand
  // (user_would_react) are never rate-limited.
  const nowMs = Date.now();
  if (key.id && reactionAllowed(decision, lastReactAt.get(jid) ?? 0, nowMs, settings.reactions.cooldown_seconds)) {
    try {
      lastReactAt.set(jid, nowMs);
      await sleep(300 + Math.random() * 600);
      const reactSent = await sock.sendMessage(jid, {
        react: { text: decision!.react!, key: { remoteJid: jid, fromMe: false, id: key.id, participant: incoming.sender } },
      });
      // Track our own reaction echo, or the agent would "learn" its own
      // reactions as operator vocabulary (self-reinforcing loop).
      if (reactSent?.key?.id) ourSentMessageIds.add(reactSent.key.id);
      console.log(`[react] ${decision!.react}${decision!.user_would_react ? " (mirroring user)" : ""} on msg ${key.id.slice(0, 10)}`);
    } catch (reactError) {
      console.error("[react] failed:", reactError);
    }
  } else if (decision?.react) {
    console.log("[react] skipped — cooldown active for this chat");
  }
  if (!decision?.reply) {
    if (decision) console.log(`[agent] no reply (${decision.reason})`);
    return;
  }

  await sendHumanLike(sock, jid, decision.reply, incomingText);
  await sendPollIfAny(sock, jid, decision);
  await deliverIfAny(sock, decision);
}

/**
 * The agent plans messages for chats other than the one being answered
 * (currently: the first reply to a just-consented chat). Deliver it the same
 * human-like way; the chat was just consented so no extra access check —
 * but a bad/missing plan is skipped, never crashed on.
 */
async function deliverIfAny(sock: WASocket, decision: AgentDecision | null): Promise<void> {
  const plan = decision?.deliver;
  if (!plan?.chat_id || !plan.text) return;
  console.log(`[deliver] -> ${plan.chat_id}`);
  await sendHumanLike(sock, plan.chat_id, plan.text, "", "agent");
}

/**
 * Attach a YES/NO consent poll to the operator console right after a consent
 * prompt. One tap = one `yes <chat>` / `no <chat>` console command; typed
 * YES/NO keeps working as the fallback regardless.
 */
async function sendConsentPoll(
  sock: WASocket,
  operatorTarget: string,
  chatId: string
): Promise<void> {
  const spec = consentPollSpec(settings);
  if (!spec) return;
  await sleep(consentPollDelayMs(settings));
  try {
    const secret = randomBytes(32);
    const sent = await sock.sendMessage(operatorTarget, {
      poll: { ...spec, messageSecret: secret },
    });
    if (sent?.key?.id) {
      registerConsentPoll(sent.key.id, chatId, secret, spec.values);
      console.log(`[consent] poll attached for ${chatId}`);
    }
  } catch (error) {
    console.error("[consent] poll send failed (typed YES/NO still works):", error);
  }
}

/** One tap on a consent poll = one targeted console command for that chat. */
async function handleConsentVote(sock: WASocket, msg: proto.IWebMessageInfo): Promise<void> {
  const vote = parseConsentVote(msg, sock);
  if (!vote) return;
  console.log(`[consent] operator voted ${vote.choice} for ${vote.chatId}`);
  const d = await askAgentConsole(`${vote.choice} ${vote.chatId}`);
  const operatorChat = msg.key.remoteJid;
  if (d?.reply && operatorChat) {
    await sendHumanLike(sock, operatorChat, d.reply);
    await deliverIfAny(sock, d);
  }
}

/**
 * A poll is an attachment to the reply turn (like tapping "create poll" right
 * after typing): short beat after the text, then the native poll. Failure to
 * send it must never disturb the already-delivered reply.
 */
async function sendPollIfAny(
  sock: WASocket,
  jid: string,
  decision: AgentDecision
): Promise<void> {
  if (!decision.poll) return;
  const pollMsg = buildPollMessage(decision.poll, settings);
  if (!pollMsg) {
    console.log("[poll] invalid poll from agent — skipped");
    return;
  }
  await sleep(pollDelayMs(settings));
  try {
    const sent = await sock.sendMessage(jid, pollMsg);
    if (sent?.key?.id) ourSentMessageIds.add(sent.key.id);
    console.log(`[poll] ${decision.poll.question.slice(0, 60)} (${pollMsg.poll.values.length} options)`);
    void reportOutgoing({
      group_id: jid,
      text: pollRecordText(decision.poll),
      timestamp: new Date().toISOString(),
      source: "agent",
    });
  } catch (error) {
    console.error("[poll] failed to send poll:", error);
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
  if (isSelfChat(groupJid, sock)) return;
  void reportOutgoing({ group_id: groupJid, text, timestamp, source });
}
