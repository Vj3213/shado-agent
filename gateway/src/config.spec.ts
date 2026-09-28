import { afterEach, describe, expect, it } from "vitest";
import { chatAllowed, config, settings } from "./config.js";

// The exported config/settings objects are plain mutable records — snapshot
// them here so tests can pin their own state regardless of the real .env.
const originalGroup = config.groupJid;
const originalPrivateChats = settings.allow_private_chats;

afterEach(() => {
  config.groupJid = originalGroup;
  settings.allow_private_chats = originalPrivateChats;
});

describe("chatAllowed", () => {
  it("allows the configured group", () => {
    config.groupJid = "120363cfg@g.us";
    expect(chatAllowed("120363cfg@g.us")).toBe(true);
  });

  it("never allows other groups", () => {
    config.groupJid = "120363cfg@g.us";
    expect(chatAllowed("999other@g.us")).toBe(false);
  });

  it("allows private chats only when the setting is on", () => {
    config.groupJid = "120363cfg@g.us";
    settings.allow_private_chats = true;
    expect(chatAllowed("919999000111@s.whatsapp.net")).toBe(true);
    settings.allow_private_chats = false;
    expect(chatAllowed("919999000111@s.whatsapp.net")).toBe(false);
  });

  it("with no configured group, only private chats can pass", () => {
    config.groupJid = undefined;
    settings.allow_private_chats = true;
    expect(chatAllowed("120363any@g.us")).toBe(false);
    expect(chatAllowed("919999000111@s.whatsapp.net")).toBe(true);
  });
});
