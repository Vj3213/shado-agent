import { connect } from "./connection.js";
import { handleMessagesUpsert } from "./handlers.js";
import { startRelayServer } from "./relay-server.js";
import type { WASocket } from "@whiskeysockets/baileys";

let sock: WASocket | null = null;

async function main(): Promise<void> {
  console.log("Starting whatsapp-agent gateway...");

  sock = await connect({
    onSocketReady: (socket) => {
      sock = socket; // keep the relay pointing at the freshest socket
      socket.ev.on("messages.upsert", (upsert) => {
        void handleMessagesUpsert(socket, upsert);
      });
    },
  });
  startRelayServer(() => sock);

  const shutdown = async (): Promise<void> => {
    console.log("\nShutting down gateway...");
    try {
      await sock?.end(undefined);
    } finally {
      process.exit(0);
    }
  };
  process.on("SIGINT", () => void shutdown());
  process.on("SIGTERM", () => void shutdown());
}

main().catch((error) => {
  console.error("Gateway failed to start:", error);
  process.exit(1);
});
