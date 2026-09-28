import { createHash, randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { decryptPollVote, jidNormalizedUser } from "@whiskeysockets/baileys";
import type { proto, WASocket } from "@whiskeysockets/baileys";
import { config } from "./config.js";
import type { Settings } from "./config.js";

/**
 * Consent polls: when the agent asks the operator to approve a chat, the
 * gateway attaches a small YES/NO poll so approval is one tap. The AGENT owns
 * consent logic; this module only handles the WhatsApp protocol:
 *   - create the poll with OUR OWN messageSecret (Baileys discards its
 *     internally generated one — votes are undecryptable without ours)
 *   - remember which chat each poll was about (poll msg id -> chat), persisted
 *     so a vote arriving after a restart can still be decrypted
 *   - decrypt the encrypted vote update and map the selected-option hash
 *     (plain SHA-256 of the option name, hex uppercase) back to YES/NO
 */

const MAX_TRACKED_POLLS = 32;
const POLL_STORE_FILE = "consent-polls.json";

interface ConsentPollRecord {
  chatId: string;
  secret: number[];
  options: string[];
}

type ConsentVote = { chatId: string; choice: "YES" | "NO" } | null;

const tracked = new Map<string, ConsentPollRecord>();

function storePath(): string {
  return join(config.authDir, POLL_STORE_FILE);
}

function loadTracked(): void {
  if (tracked.size) return;
  try {
    if (!existsSync(storePath())) return;
    const raw = JSON.parse(readFileSync(storePath(), "utf8")) as Record<string, ConsentPollRecord>;
    for (const [id, rec] of Object.entries(raw)) tracked.set(id, rec);
  } catch (error) {
    console.error("[consent] could not load consent poll store:", error);
  }
}

function saveTracked(): void {
  try {
    writeFileSync(storePath(), JSON.stringify(Object.fromEntries(tracked)));
  } catch (error) {
    console.error("[consent] could not save consent poll store:", error);
  }
}

export function consentPollSpec(settings_: Settings): { name: string; values: string[]; selectableCount: number } | null {
  const poll = settings_.consent_poll;
  if (!poll?.enabled || !poll.name?.trim() || poll.options?.filter((o) => o.trim()).length < 2) return null;
  return {
    name: poll.name.trim(),
    values: poll.options.map((o) => o.trim()),
    selectableCount: 1,
  };
}

export function consentPollDelayMs(settings_: Settings): number {
  const [min, max] = settings_.consent_poll?.delay_after_prompt_ms ?? [800, 2000];
  return min + Math.floor(Math.random() * (max - min + 1));
}

export function registerConsentPoll(pollMsgId: string, chatId: string, secret: Uint8Array, options: string[]): void {
  loadTracked();
  while (tracked.size >= MAX_TRACKED_POLLS) {
    tracked.delete(tracked.keys().next().value as string); // oldest first
  }
  tracked.set(pollMsgId, { chatId, secret: Array.from(secret), options: [...options] });
  saveTracked();
}

/** Uppercase hex of SHA-256(optionName) — exactly how the phone hashes options into votes. */
export function optionHash(optionName: string): string {
  return createHash("sha256").update(optionName, "utf8").digest("hex").toUpperCase();
}

/** Map decrypted vote bytes back to the tapped option (un-votes yield no match). */
export function choiceFromVote(selectedOptions: Uint8Array[] | undefined, options: string[]): string | null {
  if (!selectedOptions?.length) return null;
  const votedHexes = selectedOptions.map((b) => Buffer.from(b).toString("hex").toUpperCase());
  for (const option of options) {
    if (votedHexes.includes(optionHash(option))) return option;
  }
  return null;
}

function tryDecrypt(
  update: proto.Message.IPollUpdateMessage,
  record: ConsentPollRecord,
  sock: WASocket,
  voterCandidates: string[]
): proto.Message.PollVoteMessage | null {
  // WhatsApp encrypts votes bound to specific JID formats. Documented working
  // combo in 1:1 chats: creator = LID, voter = PN — but which representation
  // we hold depends on account generation, so try the plausible pairs.
  const creators: string[] = [];
  if (sock.user?.lid) creators.push(jidNormalizedUser(sock.user.lid));
  if (sock.user?.id) creators.push(jidNormalizedUser(sock.user.id));
  const secret = Uint8Array.from(record.secret);
  const tried: string[] = [];
  for (const pollCreatorJid of creators) {
    for (const voterJid of voterCandidates) {
      try {
        return decryptPollVote(update.vote as proto.Message.IPollEncValue, {
          pollCreatorJid,
          pollMsgId: update.pollCreationMessageKey?.id as string,
          pollEncKey: secret,
          voterJid,
        });
      } catch {
        tried.push(`${pollCreatorJid} × ${voterJid}`);
      }
    }
  }
  console.error(`[consent] decryption failed for combos: ${tried.join(" | ")}`);
  return null;
}

/**
 * Handle one messages.upsert entry that carries a pollUpdateMessage.
 * Returns the parsed consent vote (or null when the poll is not ours /
 * cannot be decrypted / the vote was retracted).
 */
export function parseConsentVote(
  msg: proto.IWebMessageInfo,
  sock: WASocket
): ConsentVote {
  const update = msg.message?.pollUpdateMessage;
  const pollMsgId = update?.pollCreationMessageKey?.id;
  if (!update || !pollMsgId) return null;
  loadTracked();
  const record = tracked.get(pollMsgId);
  if (!record) return null; // someone else's poll (or lost across restarts)

  const voterCandidates = [
    ...new Set(
      [
        // The vote's chat key is usually the operator's LID — often NOT the
        // format the encryption binds to. The plain number (OPERATOR_PHONE)
        // gives the PN form, which is what 1:1 votes decrypt against.
        msg.key.participant,
        msg.key.remoteJid,
        config.operatorPhone ? `${config.operatorPhone.replace(/[^0-9]/g, "")}@s.whatsapp.net` : undefined,
      ]
        .filter((j): j is string => !!j)
        .map((j) => jidNormalizedUser(j))
    ),
  ];
  const vote = tryDecrypt(update, record, sock, voterCandidates);
  if (!vote) {
    console.error(`[consent] could not decrypt vote for poll ${pollMsgId} — typed YES/NO still works`);
    return null;
  }
  const choice = choiceFromVote(vote.selectedOptions as Uint8Array[], record.options);
  return choice ? { chatId: record.chatId, choice: choice as "YES" | "NO" } : null;
}
