import http from "node:http";
import type { WASocket } from "@whiskeysockets/baileys";
import { chatAllowed, config } from "./config.js";
import { askAgentInitiate } from "./agent-client.js";
import { sendHumanLike } from "./handlers.js";

type GetSocket = () => WASocket | null;

/**
 * Tiny local HTTP relay so the operator (or a script) can make the bot act:
 *   POST /send     {chat_id, text}   — send a message into a chat
 *   POST /initiate {chat_id}         — agent composes an opener, gateway sends it
 * Binds to 127.0.0.1 only — nothing external can reach it.
 */
export function startRelayServer(getSocket: GetSocket): void {
  const server = http.createServer(async (req, res) => {
    const respond = (status: number, body: object) => {
      res.writeHead(status, { "content-type": "application/json" });
      res.end(JSON.stringify(body));
    };
    if (req.method !== "POST") {
      respond(404, { error: "not found" });
      return;
    }

    let raw = "";
    req.on("data", (chunk: string) => (raw += chunk));
    req.on("end", async () => {
      const sock = getSocket();
      if (!sock) {
        respond(503, { error: "whatsapp socket not ready" });
        return;
      }
      let body: { chat_id?: string; text?: string };
      try {
        body = JSON.parse(raw);
      } catch {
        respond(400, { error: "invalid json" });
        return;
      }
      const chatId = body.chat_id ?? "";
      if (!chatId || !chatAllowed(chatId)) {
        respond(403, { error: "chat not allowlisted" });
        return;
      }

      try {
        if (req.url === "/send") {
          const text = (body.text ?? "").trim();
          if (!text) {
            respond(400, { error: "text required" });
            return;
          }
          console.log(`[relay] send -> ${chatId}`);
          await sendHumanLike(sock, chatId, text, "", "operator");
          respond(200, { sent: true });
          return;
        }
        if (req.url === "/initiate") {
          console.log(`[relay] initiate -> ${chatId}`);
          const decision = await askAgentInitiate(chatId);
          if (!decision?.reply) {
            respond(200, { sent: false, reason: decision?.reason ?? "agent unavailable" });
            return;
          }
          await sendHumanLike(sock, chatId, decision.reply);
          respond(200, { sent: true, text: decision.reply });
          return;
        }
        respond(404, { error: "not found" });
      } catch (error) {
        console.error("[relay] request failed:", error);
        respond(500, { error: "send failed" });
      }
    });
  });

  server.listen(config.gatewayPort, "127.0.0.1", () => {
    console.log(`✔ Relay server listening on http://127.0.0.1:${config.gatewayPort}`);
  });
}
