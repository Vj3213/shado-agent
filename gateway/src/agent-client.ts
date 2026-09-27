import { config } from "./config.js";
import type { AgentDecision, IncomingMessage, OutgoingSource } from "./types.js";

// Headroom for the agent's model-fallback chain: up to 2 rounds × 3 models,
// each attempt bounded to 15s inside the agent, plus pauses (~111s worst case).
const INCOMING_TIMEOUT_MS = 120_000;

/**
 * Thin HTTP boundary to the Python agent. All agent intelligence lives
 * behind this call; the gateway never parses or interprets message text.
 */
export async function askAgent(msg: IncomingMessage): Promise<AgentDecision | null> {
  try {
    const response = await fetch(`${config.agentUrl}/messages/incoming`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(msg),
      signal: AbortSignal.timeout(INCOMING_TIMEOUT_MS),
    });
    if (!response.ok) {
      console.error(`[agent-client] agent returned HTTP ${response.status}`);
      return null;
    }
    return (await response.json()) as AgentDecision;
  } catch (error) {
    console.error("[agent-client] failed to reach agent:", error);
    return null;
  }
}

/** Operator console: commands typed in the bot phone's self-chat. */
export async function askAgentConsole(text: string): Promise<AgentDecision | null> {
  try {
    const response = await fetch(`${config.agentUrl}/messages/console`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
      signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) {
      console.error(`[agent-client] console returned HTTP ${response.status}`);
      return null;
    }
    return (await response.json()) as AgentDecision;
  } catch (error) {
    console.error("[agent-client] console call failed:", error);
    return null;
  }
}

/** Record a message we ourselves sent; returns the agent's decision, which
 * may carry a consent prompt when the operator messaged a brand-new chat. */
export async function reportOutgoing(
  msg: Omit<IncomingMessage, "sender"> & { source: OutgoingSource }
): Promise<AgentDecision | null> {
  try {
    const response = await fetch(`${config.agentUrl}/messages/outgoing`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(msg),
      signal: AbortSignal.timeout(15_000),
    });
    if (!response.ok) {
      console.error(`[agent-client] outgoing returned HTTP ${response.status}`);
      return null;
    }
    return (await response.json()) as AgentDecision;
  } catch (error) {
    console.error("[agent-client] failed to report outgoing message:", error);
    return null;
  }
}

/** Ask the agent to compose a proactive opener for a chat (bot-initiated). */
export async function askAgentInitiate(chatId: string): Promise<AgentDecision | null> {
  try {
    const response = await fetch(`${config.agentUrl}/messages/initiate`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ chat_id: chatId }),
      signal: AbortSignal.timeout(INCOMING_TIMEOUT_MS),
    });
    if (!response.ok) {
      console.error(`[agent-client] initiate returned HTTP ${response.status}`);
      return null;
    }
    return (await response.json()) as AgentDecision;
  } catch (error) {
    console.error("[agent-client] initiate failed:", error);
    return null;
  }
}
