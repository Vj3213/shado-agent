/** Wire-format types shared between gateway modules. */

/** Payload the gateway forwards to the agent for every allowlisted-group message. */
export interface IncomingMessage {
  group_id: string;
  sender: string;
  text: string;
  /** ISO-8601 timestamp taken from the WhatsApp message. */
  timestamp: string;
  /** Sender's self-declared WhatsApp display name (may be missing). */
  sender_name?: string | null;
  /** Sender's phone number digits, when resolvable (group metadata bridge). */
  sender_number?: string | null;
}

/** Who authored an outgoing message — operator style samples vs agent output. */
export type OutgoingSource = "operator" | "agent";

/** A native WhatsApp poll the agent attached to its reply. */
export interface PollSpec {
  question: string;
  options: string[];
}

/** A message the agent plans for a chat OTHER than the one being answered —
 * e.g. the first reply to a chat the operator just consented to. */
export interface DeliverPlan {
  chat_id: string;
  text: string;
}

/** The agent's decision: reply text (or null) plus why. */
export interface AgentDecision {
  reply: string | null;
  reason: string;
  /** Non-null: text the gateway must deliver to the operator's self-chat. */
  self_prompt?: string | null;
  /** Emoji to tap-react on the message that triggered this decision. */
  react?: string | null;
  /** True: the model is confident the USER would have tapped this reaction
   * themselves — mirroring their hand, never rate-limited. */
  user_would_react?: boolean;
  /** Non-null: a poll to send right after the reply text. */
  poll?: PollSpec | null;
  /** Non-null: a message to deliver into a different chat. */
  deliver?: DeliverPlan | null;
}
