import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
  type WASocket,
} from "@whiskeysockets/baileys";
import { Boom } from "@hapi/boom";
import { pino } from "pino";
import { config } from "./config.js";

export interface ConnectionCallbacks {
  onSocketReady: (sock: WASocket) => void;
}

/**
 * Creates the Baileys socket and owns the connect/reconnect lifecycle.
 * Login is as a LINKED DEVICE (like WhatsApp Web) — QR by default, or a
 * pairing code when LINK_PHONE_NUMBER is set. Session persists in gateway/auth/.
 */
export async function connect(callbacks: ConnectionCallbacks): Promise<WASocket> {
  const { state, saveCreds } = await useMultiFileAuthState(config.authDir);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    auth: state,
    printQRInTerminal: false,
    browser: Browsers.macOS("Chrome"),
    syncFullHistory: false,
    markOnlineOnConnect: false,
    // Baileys' internal logger is very chatty (raw sync/decrypt noise).
    // Our own handlers log everything that actually matters.
    logger: pino({ level: "silent" }),
  });

  sock.ev.on("creds.update", saveCreds);

  // Delivery lifecycle for our own outgoing messages: [sent] only means Baileys
  // queued it; these statuses tell us whether WhatsApp actually took it.
  const STATUS_LABELS: Record<number, string> = {
    0: "error", 1: "pending", 2: "server-ack", 3: "delivered", 4: "read", 5: "played",
  };
  sock.ev.on("messages.update", (updates) => {
    for (const { key, update } of updates) {
      if (!key.fromMe || update.status === undefined || update.status === null) continue;
      const label = STATUS_LABELS[update.status] ?? `status-${update.status}`;
      console.log(`[status] ${label} (msg ${key.id?.slice(0, 10) ?? "?"})`);
    }
  });

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      if (config.linkPhoneNumber) {
        // Pairing-code login: display a short code to type into WhatsApp > Linked devices.
        const code = await sock.requestPairingCode(config.linkPhoneNumber);
        console.log(`\n🔗 Pairing code for ${config.linkPhoneNumber}: ${code}\n`);
      } else {
        const { default: qrcode } = await import("qrcode-terminal");
        qrcode.generate(qr, { small: true });
        console.log("Scan the QR above with WhatsApp > Linked devices.");
      }
    }

    if (connection === "open") {
      console.log("✅ Connected to WhatsApp.");
      callbacks.onSocketReady(sock);
    }

    if (connection === "close") {
      const statusCode = (lastDisconnect?.error as Boom)?.output?.statusCode;
      const shouldReconnect = statusCode !== DisconnectReason.loggedOut;
      console.log(
        `⚠️  Connection closed (${statusCode ?? "unknown"}). ${
          shouldReconnect ? "Reconnecting..." : "Logged out — delete gateway/auth/ and re-link."
        }`
      );
      if (shouldReconnect) {
        await connect(callbacks);
      }
    }
  });

  return sock;
}
