import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clearGroupMetaCacheForTests, senderInfo } from "./group-meta.js";
import type { WASocket } from "@whiskeysockets/baileys";

const metadata = (participants: unknown[]) => ({
  groupMetadata: vi.fn().mockResolvedValue({ participants }),
});

const participant = {
  id: "555lid@lid",
  lid: "555lid@lid",
  jid: "919876543210@s.whatsapp.net",
  name: "Rohit Sharma", // the OPERATOR's saved name
  notify: "⚡Rohit⚡",   // the contact's self-set name
};

beforeEach(() => clearGroupMetaCacheForTests());

describe("senderInfo", () => {
  it("resolves number + saved name from group metadata", async () => {
    const sock = metadata([participant]) as unknown as WASocket;
    const info = await senderInfo(sock, "120363fam@g.us", "555lid@lid");
    expect(info).toEqual({
      number: "919876543210",
      savedName: "Rohit Sharma",
      notifyName: "⚡Rohit⚡",
    });
  });

  it("caches metadata per group (second call does not refetch)", async () => {
    const sock = metadata([participant]);
    await senderInfo(sock as unknown as WASocket, "120363fam@g.us", "555lid@lid");
    await senderInfo(sock as unknown as WASocket, "120363fam@g.us", "555lid@lid");
    expect(sock.groupMetadata).toHaveBeenCalledTimes(1);
  });

  it("returns null (and keeps the path alive) when metadata fails", async () => {
    const sock = {
      groupMetadata: vi.fn().mockRejectedValue(new Error("offline")),
    } as unknown as WASocket;
    expect(await senderInfo(sock, "120363fam@g.us", "555lid@lid")).toBeNull();
  });

  it("returns null for participants that are not in the group", async () => {
    const sock = metadata([participant]) as unknown as WASocket;
    expect(await senderInfo(sock, "120363fam@g.us", "999stranger@lid")).toBeNull();
  });
});
