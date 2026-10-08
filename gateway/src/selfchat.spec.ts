import { describe, expect, it } from "vitest";
import { isSelfChat } from "./handlers.js";
import type { WASocket } from "@whiskeysockets/baileys";

const sock = {
  user: {
    id: "919599621076:77@s.whatsapp.net", // PN form, with device suffix
    lid: "86328926560256:77@lid",         // LID form, with device suffix
  },
} as unknown as WASocket;

describe("isSelfChat", () => {
  it("recognizes the self-chat in the PN form (device suffix stripped)", () => {
    expect(isSelfChat("919599621076@s.whatsapp.net", sock)).toBe(true);
  });

  it("recognizes the self-chat in the LID form (device suffix stripped)", () => {
    expect(isSelfChat("86328926560256@lid", sock)).toBe(true);
  });

  it("never mistakes other people for the console", () => {
    expect(isSelfChat("115435953516554@lid", sock)).toBe(false);
    expect(isSelfChat("919999000111@s.whatsapp.net", sock)).toBe(false);
  });

  it("degrades gracefully when the socket has no identity yet", () => {
    expect(isSelfChat("86328926560256@lid", {} as WASocket)).toBe(false);
  });
});
