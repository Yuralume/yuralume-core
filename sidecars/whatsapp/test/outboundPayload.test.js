import assert from "node:assert/strict";
import test from "node:test";
import { buildOutboundPayloads, toWhatsAppJid } from "../src/outboundPayload.js";

test("keeps explicit WhatsApp JID and normalizes phone numbers", () => {
  assert.equal(toWhatsAppJid("12025550123@s.whatsapp.net"), "12025550123@s.whatsapp.net");
  assert.equal(toWhatsAppJid("+1 202 555 0123"), "12025550123@s.whatsapp.net");
});

test("builds text-only payload", () => {
  assert.deepEqual(buildOutboundPayloads({ text: "hello" }), [{ text: "hello" }]);
});

test("uses first image attachment caption for text", () => {
  const payloads = buildOutboundPayloads({
    text: "caption",
    attachments: [{ kind: "image", url: "https://example.test/a.png", mime_type: "image/png" }],
  });

  assert.deepEqual(payloads, [
    {
      image: { url: "https://example.test/a.png" },
      caption: "caption",
      mimetype: "image/png",
    },
  ]);
});
