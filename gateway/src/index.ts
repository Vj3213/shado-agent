import { connect } from "./connection.js";
import { handleMessagesUpsert } from "./handlers.js";
import { startRelayServer } from "./relay-server.js";
import type { WASocket } from "@whiskeysockets/baileys";

// libsignal prints reconnect/crypto noise via raw console calls, bypassing loggers.
// Filter only its known-noisy patterns; everything else passes through.
const NOISY_PREFIXES = [
  "Closing session:",
  "Failed to decrypt message with any known session",
  "Session error:",
  "Decrypted message with closed session",
];
for (const method of ["info", "error", "warn"] as const) {
  const original = console[method].bind(console);
  console[method] = (...args: unknown[]) => {
    const first = args[0];
    if (typeof first === "string" && NOISY_PREFIXES.some((p) => first.startsWith(p))) return;
    original(...args);
  };
}

let sock: WASocket | null = null;

async function main(): Promise<void> {
  console.log("Starting shado-agent gateway...");

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
