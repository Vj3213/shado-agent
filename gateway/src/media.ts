import { downloadMediaMessage } from "@whiskeysockets/baileys";
import type { proto, WASocket } from "@whiskeysockets/baileys";
import { settings } from "./config.js";

export interface MediaForward {
  kind: "image" | "sticker" | "gif";
  mimetype: string;
  /** base64 of the bytes to send (real bytes for images/stickers, the
   * jpegThumbnail frame for GIFs/videos). */
  base64: string;
  caption: string | null;
}

export function detectMedia(msg: proto.IWebMessageInfo): MediaForward | null {
  if (settings.media?.reply_to_media === false) return null;
  const m = msg.message;
  if (!m) return null;
  const caption = (it: { caption?: string | null } | undefined) => (it?.caption ?? "").trim() || null;
  if (m.imageMessage) {
    return { kind: "image", mimetype: m.imageMessage.mimetype ?? "image/jpeg", caption: caption(m.imageMessage), base64: "" };
  }
  if (m.stickerMessage) {
    return { kind: "sticker", mimetype: m.stickerMessage.mimetype ?? "image/webp", caption: null, base64: "" };
  }
  if (m.videoMessage) {
    const thumb = m.videoMessage.jpegThumbnail;
    if (!thumb) return null;
    return {
      kind: m.videoMessage.gifPlayback ? "gif" : "image",
      mimetype: "image/jpeg",
      caption: caption(m.videoMessage),
      base64: Buffer.from(thumb).toString("base64"),
    };
  }
  return null;
}

export async function resolveMediaBytes(
  msg: proto.IWebMessageInfo,
  sock: WASocket,
  forward: MediaForward
): Promise<MediaForward | null> {
  const maxBytes = (settings.media?.max_mb ?? 10) * 1024 * 1024;
  if (forward.base64) return forward; // inline thumbnail already present
  try {
    // NO reuploadRequest: the media re-upload request can hang forever with
    // certain session states (the 100%-timeout case) — better to fail FAST
    // and fall back to the inline preview than to wedge the loop.
    const buffer = await withTimeout(
      downloadMediaMessage(msg, "buffer", {}),
      mediaDownloadTimeoutMs(),
      "media download"
    );
    if (buffer && buffer.length <= maxBytes) {
      return { ...forward, base64: buffer.toString("base64") };
    }
    console.log(`[media] ${forward.kind} skipped — missing or over ${settings.media?.max_mb ?? 10}MB`);
  } catch (error) {
    console.error("[media] direct download failed:", error);
  }
  return lowResFallback(msg, forward);
}

/** Every image/sticker message carries a small jpeg preview inline — when the
 * full download refuses, forward THAT so the agent still sees the gist. */
export function lowResFallback(msg: proto.IWebMessageInfo, forward: MediaForward): MediaForward | null {
  const content = msg.message;
  const thumb = content?.imageMessage?.jpegThumbnail ?? content?.stickerMessage?.pngThumbnail ?? null;
  if (!thumb || forward.kind !== "image" && forward.kind !== "sticker") return null;
  console.log("[media] using low-res inline preview instead of full download");
  return { ...forward, base64: Buffer.from(thumb).toString("base64"), mimetype: "image/jpeg" };
}

// A wedged media download would block the WHOLE sequential message loop
// forever (Ved's dead-gateway incident) — every await gets a hard timeout.
const mediaDownloadTimeoutMs = () => (settings.media?.download_timeout_seconds ?? 45) * 1000;

export function withTimeout<T>(promise: Promise<T>, ms: number, tag: string): Promise<T | null> {
  // Pre-catch the guarded promise: if it REJECTS after the timeout already
  // resolved, the rejection would be unhandled and kill the process (the
  // dead-gateway crash-loop incident).
  const guarded = promise.catch((error) => {
    console.error(`[media] ${tag} failed:`, error);
    return null;
  });
  return Promise.race([
    guarded,
    new Promise<null>((resolve) =>
      setTimeout(() => {
        console.error(`[media] ${tag} timed out after ${ms}ms — skipping`);
        resolve(null);
      }, ms)
    ),
  ]);
}
