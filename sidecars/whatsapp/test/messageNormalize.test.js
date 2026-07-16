import assert from "node:assert/strict";
import test from "node:test";
import { extractMediaParts, normalizeIncomingMessage } from "../src/messageNormalize.js";

test("normalizes text message into Kokoro sidecar event", () => {
  const event = normalizeIncomingMessage({
    key: {
      remoteJid: "12025550123@s.whatsapp.net",
      id: "ABC",
      participant: "12025550123@s.whatsapp.net",
    },
    messageTimestamp: 1710000000,
    message: { conversation: " hello " },
  });

  assert.equal(event.id, "ABC");
  assert.equal(event.chat_ref, "12025550123@s.whatsapp.net");
  assert.equal(event.sender_ref, "12025550123@s.whatsapp.net");
  assert.equal(event.text, "hello");
  assert.equal(event.timestamp, "2024-03-09T16:00:00.000Z");
});

test("unwraps ephemeral image captions and reports media parts", () => {
  const message = {
    key: { remoteJid: "group@g.us", id: "IMG", participant: "user@s.whatsapp.net" },
    message: {
      ephemeralMessage: {
        message: {
          imageMessage: {
            caption: "photo caption",
            mimetype: "image/jpeg",
          },
        },
      },
    },
  };
  const event = normalizeIncomingMessage(message, { mediaUrls: ["http://sidecar/media.jpg"] });

  assert.equal(event.text, "photo caption");
  assert.deepEqual(event.media_urls, ["http://sidecar/media.jpg"]);
  assert.deepEqual(extractMediaParts(message), [{ type: "imageMessage", mimeType: "image/jpeg" }]);
});

test("uses sensible default mimetypes for image and sticker media", () => {
  assert.deepEqual(
    extractMediaParts({
      key: { remoteJid: "user@s.whatsapp.net", id: "IMG" },
      message: { imageMessage: {} },
    }),
    [{ type: "imageMessage", mimeType: "image/jpeg" }],
  );
  assert.deepEqual(
    extractMediaParts({
      key: { remoteJid: "user@s.whatsapp.net", id: "STICKER" },
      message: { stickerMessage: {} },
    }),
    [{ type: "stickerMessage", mimeType: "image/webp" }],
  );
});
