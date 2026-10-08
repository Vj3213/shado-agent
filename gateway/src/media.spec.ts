import { describe, expect, it } from "vitest";
import { detectMedia, lowResFallback } from "./media.js";
import type { proto } from "@whiskeysockets/baileys";

const msg = (over: Record<string, unknown>) =>
  ({ ...over }) as unknown as proto.IWebMessageInfo;

describe("detectMedia", () => {
  it("detects captioned images and carries the caption", () => {
    const m = msg({ message: { imageMessage: { mimetype: "image/jpeg", caption: "bhai dekho" } } });
    expect(detectMedia(m)).toMatchObject({
      kind: "image",
      mimetype: "image/jpeg",
      caption: "bhai dekho",
    });
  });

  it("detects stickers (webp, no caption)", () => {
    const m = msg({ message: { stickerMessage: { mimetype: "image/webp" } } });
    expect(detectMedia(m)).toMatchObject({ kind: "sticker", caption: null });
  });

  it("uses the inline thumbnail for GIFs and videos (no big download)", () => {
    const thumb = Buffer.from("jpegframe");
    const m = msg({
      message: { videoMessage: { gifPlayback: true, jpegThumbnail: thumb } },
    });
    const detected = detectMedia(m);
    expect(detected).toMatchObject({ kind: "gif", mimetype: "image/jpeg" });
    expect(Buffer.from(detected!.base64, "base64").toString()).toBe("jpegframe");
  });

  it("returns null for plain video (v1: not forwarded)", () => {
    const m = msg({ message: { videoMessage: { gifPlayback: false } } });
    expect(detectMedia(m)).toBeNull();
  });

  it("returns null for plain text", () => {
    expect(detectMedia(msg({ message: { conversation: "hi" } }))).toBeNull();
    expect(detectMedia(msg({ message: null }))).toBeNull();
  });
});

describe("lowResFallback", () => {
  it("falls back to the image's inline preview when the download refuses", () => {
    const thumb = Buffer.from("low-res-preview");
    const m = msg({ message: { imageMessage: { jpegThumbnail: new Uint8Array(thumb) } } });
    const forward = { kind: "image" as const, mimetype: "image/jpeg", base64: "", caption: null };
    const out = lowResFallback(m, forward);
    expect(out).toMatchObject({ mimetype: "image/jpeg" });
    expect(Buffer.from(out!.base64, "base64").toString()).toBe("low-res-preview");
  });

  it("keeps the caption on fallback", () => {
    const m = msg({ message: { imageMessage: { jpegThumbnail: new Uint8Array([1, 2]) } } });
    const forward = { kind: "image" as const, mimetype: "image/jpeg", base64: "", caption: "dinner idea?" };
    expect(lowResFallback(m, forward)!.caption).toBe("dinner idea?");
  });

  it("returns null when no preview exists", () => {
    const m = msg({ message: { imageMessage: {} } });
    const forward = { kind: "image" as const, mimetype: "image/jpeg", base64: "", caption: null };
    expect(lowResFallback(m, forward)).toBeNull();
  });
});
