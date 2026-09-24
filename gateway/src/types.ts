/** Wire-format types shared between gateway modules. */

/** Payload the gateway forwards to the agent for every allowlisted-group message. */
export interface IncomingMessage {
  group_id: string;
  sender: string;
  text: string;
  /** ISO-8601 timestamp taken from the WhatsApp message. */
  timestamp: string;
}

/** The agent's decision: reply text (or null) plus why. */
export interface AgentDecision {
  reply: string | null;
  reason: string;
}
