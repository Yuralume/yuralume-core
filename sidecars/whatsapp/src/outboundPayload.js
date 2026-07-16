export function toWhatsAppJid(value) {
  const raw = String(value ?? "").trim();
  if (!raw) {
    throw new Error("chat_ref is required");
  }
  if (raw.includes("@")) {
    return raw;
  }
  const digits = raw.replace(/[^\d]/g, "");
  if (!digits) {
    throw new Error("chat_ref is invalid");
  }
  return `${digits}@s.whatsapp.net`;
}

export function buildOutboundPayloads({ text = "", attachments = [] }) {
  const cleanText = String(text ?? "").trim();
  const cleanAttachments = Array.isArray(attachments) ? attachments.filter((item) => item?.url) : [];
  if (cleanAttachments.length === 0) {
    if (!cleanText) {
      throw new Error("text or attachments are required");
    }
    return [{ text: cleanText }];
  }

  const payloads = [];
  let textConsumed = false;
  for (const attachment of cleanAttachments) {
    const caption = String(attachment.caption ?? "").trim()
      || (!textConsumed ? cleanText : undefined);
    payloads.push(buildMediaPayload(attachment, caption));
    textConsumed = textConsumed || Boolean(caption && caption === cleanText);
  }
  if (cleanText && !textConsumed) {
    payloads.push({ text: cleanText });
  }
  return payloads;
}

function buildMediaPayload(attachment, caption) {
  const url = String(attachment.url).trim();
  const mimeType = String(attachment.mime_type ?? attachment.mimeType ?? "").toLowerCase();
  const kind = String(attachment.kind ?? "").toLowerCase();
  if (mimeType.startsWith("image/") || kind === "image" || kind === "photo") {
    return { image: { url }, caption: caption || undefined, mimetype: mimeType || undefined };
  }
  if (mimeType.startsWith("video/") || kind === "video") {
    return { video: { url }, caption: caption || undefined, mimetype: mimeType || undefined };
  }
  if (mimeType.startsWith("audio/") || kind === "audio") {
    return { audio: { url }, mimetype: mimeType || undefined };
  }
  return {
    document: { url },
    caption: caption || undefined,
    mimetype: mimeType || "application/octet-stream",
    fileName: attachment.file_name || attachment.fileName || "attachment",
  };
}
