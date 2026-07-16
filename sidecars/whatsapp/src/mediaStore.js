import fs from "node:fs/promises";
import path from "node:path";
import { normalizeSessionId } from "./sessionId.js";

const MIME_EXTENSIONS = new Map([
  ["image/jpeg", ".jpg"],
  ["image/png", ".png"],
  ["image/webp", ".webp"],
  ["image/gif", ".gif"],
  ["video/mp4", ".mp4"],
  ["audio/mpeg", ".mp3"],
  ["audio/ogg", ".ogg"],
  ["audio/opus", ".opus"],
  ["application/pdf", ".pdf"],
]);

export class MediaStore {
  constructor({ rootDir, baseUrl }) {
    this.rootDir = path.resolve(rootDir);
    this.baseUrl = baseUrl.replace(/\/+$/, "");
  }

  async put({ sessionId, messageId, buffer, mimeType }) {
    const safeSessionId = normalizeSessionId(sessionId);
    const ext = extensionForMime(mimeType);
    const safeMessageId = sanitizeFilePart(messageId || cryptoRandomPart());
    const filename = `${Date.now()}-${safeMessageId}${ext}`;
    const sessionDir = path.join(this.rootDir, safeSessionId);
    await fs.mkdir(sessionDir, { recursive: true });
    const target = path.join(sessionDir, filename);
    await fs.writeFile(target, buffer, { mode: 0o600 });
    return {
      filename,
      path: target,
      url: `${this.baseUrl}/sessions/${encodeURIComponent(safeSessionId)}/media/${encodeURIComponent(filename)}`,
      mimeType: mimeType || "application/octet-stream",
    };
  }

  resolve({ sessionId, filename }) {
    const safeSessionId = normalizeSessionId(sessionId);
    const safeFilename = path.basename(String(filename ?? ""));
    if (!safeFilename || safeFilename === "." || safeFilename === "..") {
      throw new Error("filename is invalid");
    }
    const resolved = path.resolve(this.rootDir, safeSessionId, safeFilename);
    const sessionRoot = path.resolve(this.rootDir, safeSessionId);
    if (resolved !== sessionRoot && !resolved.startsWith(`${sessionRoot}${path.sep}`)) {
      throw new Error("filename is invalid");
    }
    return resolved;
  }
}

export function extensionForMime(mimeType) {
  const normalized = String(mimeType ?? "").split(";")[0].trim().toLowerCase();
  return MIME_EXTENSIONS.get(normalized) ?? ".bin";
}

function sanitizeFilePart(value) {
  const cleaned = String(value).replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "");
  return cleaned || cryptoRandomPart();
}

function cryptoRandomPart() {
  return Math.random().toString(36).slice(2, 12);
}
