const WRAPPER_KEYS = [
  "ephemeralMessage",
  "viewOnceMessage",
  "viewOnceMessageV2",
  "viewOnceMessageV2Extension",
  "documentWithCaptionMessage",
  "groupMentionedMessage",
  "botInvokeMessage",
];

const MEDIA_KEYS = [
  "imageMessage",
  "videoMessage",
  "audioMessage",
  "documentMessage",
  "stickerMessage",
];

export function normalizeIncomingMessage(waMessage, { mediaUrls = [] } = {}) {
  const key = waMessage?.key ?? {};
  const chatRef = key.remoteJid;
  const messageId = key.id;
  if (!chatRef || !messageId) {
    return null;
  }
  const content = unwrapContent(waMessage.message);
  const text = extractText(content);
  return {
    id: messageId,
    message_id: messageId,
    chat_ref: chatRef,
    remote_jid: chatRef,
    sender_ref: key.participant || key.remoteJid,
    participant: key.participant || "",
    from_me: Boolean(key.fromMe),
    text: text ?? "",
    timestamp: timestampToIso(waMessage.messageTimestamp),
    media_urls: mediaUrls,
    attachment_urls: mediaUrls,
    raw_type: detectContentType(content),
  };
}

export function extractMediaParts(waMessage) {
  const content = unwrapContent(waMessage?.message);
  if (!content || typeof content !== "object") {
    return [];
  }
  const parts = [];
  for (const key of MEDIA_KEYS) {
    const value = content[key];
    if (value && typeof value === "object") {
      parts.push({
        type: key,
        mimeType: value.mimetype || defaultMimeForType(key),
      });
    }
  }
  return parts;
}

export function unwrapContent(message) {
  let current = message;
  for (let depth = 0; depth < 5; depth += 1) {
    if (!current || typeof current !== "object") {
      return current;
    }
    const wrapperKey = WRAPPER_KEYS.find((key) => current[key]?.message);
    if (!wrapperKey) {
      return current;
    }
    current = current[wrapperKey].message;
  }
  return current;
}

function extractText(content) {
  if (!content || typeof content !== "object") {
    return undefined;
  }
  const candidates = [
    content.conversation,
    content.extendedTextMessage?.text,
    content.imageMessage?.caption,
    content.videoMessage?.caption,
    content.documentMessage?.caption,
    content.buttonsResponseMessage?.selectedDisplayText,
    content.listResponseMessage?.title,
    content.templateButtonReplyMessage?.selectedDisplayText,
    content.interactiveResponseMessage?.body?.text,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) {
      return candidate.trim();
    }
  }
  if (content.locationMessage) {
    const { degreesLatitude, degreesLongitude, name, address } = content.locationMessage;
    const label = [name, address].filter(Boolean).join(" ");
    return label || `Location: ${degreesLatitude}, ${degreesLongitude}`;
  }
  if (content.contactMessage || content.contactsArrayMessage) {
    return "[使用者傳來一張聯絡人卡片]";
  }
  return undefined;
}

function detectContentType(content) {
  if (!content || typeof content !== "object") {
    return "unknown";
  }
  return Object.keys(content).find((key) => content[key] != null) ?? "unknown";
}

function timestampToIso(value) {
  if (!value) {
    return new Date().toISOString();
  }
  if (typeof value === "number") {
    return new Date(value < 10_000_000_000 ? value * 1000 : value).toISOString();
  }
  if (typeof value === "bigint") {
    const numeric = Number(value);
    return new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric).toISOString();
  }
  if (typeof value === "object") {
    const numeric = Number(value.low ?? value.toNumber?.() ?? value);
    if (Number.isFinite(numeric)) {
      return new Date(numeric < 10_000_000_000 ? numeric * 1000 : numeric).toISOString();
    }
  }
  return new Date().toISOString();
}

function defaultMimeForType(type) {
  if (type === "imageMessage") {
    return "image/jpeg";
  }
  if (type === "stickerMessage") {
    return "image/webp";
  }
  if (type === "videoMessage") {
    return "video/mp4";
  }
  if (type === "audioMessage") {
    return "audio/ogg";
  }
  return "application/octet-stream";
}
