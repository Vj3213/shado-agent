import { describe, expect, it } from "vitest";
import type { proto } from "@whiskeysockets/baileys";
import { extractText } from "./handlers.js";

type AnyMessage = Record<string, unknown>;

const msg = (message: AnyMessage | null) =>
  ({ message, key: {} }) as unknown as proto.IWebMessageInfo;

describe("extractText", () => {
  it("reads plain conversation text", () => {
    expect(extractText(msg({ conversation: "hello" }))).toBe("hello");
  });

  it("reads extended text (replies/links)", () => {
    expect(extractText(msg({ extendedTextMessage: { text: "quoted reply" } }))).toBe("quoted reply");
  });

  it("reads image and video captions", () => {
    expect(extractText(msg({ imageMessage: { caption: "pic caption" } }))).toBe("pic caption");
    expect(extractText(msg({ videoMessage: { caption: "vid caption" } }))).toBe("vid caption");
  });

  it("prefers conversation text over captions", () => {
    const both = msg({ conversation: "plain", imageMessage: { caption: "cap" } });
    expect(extractText(both)).toBe("plain");
  });

  it("returns null for non-text shapes (stickers, reactions, no body)", () => {
    expect(extractText(msg({ reactionMessage: { text: "👍" } }))).toBeNull();
    expect(extractText(msg({}))).toBeNull();
    expect(extractText(msg(null))).toBeNull();
  });
});
