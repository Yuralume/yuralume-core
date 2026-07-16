import fs from "node:fs/promises";
import path from "node:path";
import makeWASocket, {
  DisconnectReason,
  downloadMediaMessage,
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import pino from "pino";
import qrcode from "qrcode-terminal";
import { extractMediaParts, normalizeIncomingMessage } from "./messageNormalize.js";
import { buildOutboundPayloads, toWhatsAppJid } from "./outboundPayload.js";
import { normalizeSessionId } from "./sessionId.js";

const LOGGED_OUT_STATUS = DisconnectReason?.loggedOut ?? 401;

export class BaileysSession {
  constructor({ sessionId, authRootDir, mediaStore, eventHub, logger, reconnectDelayMs = 5000 }) {
    this.sessionId = normalizeSessionId(sessionId);
    this.authRootDir = authRootDir;
    this.mediaStore = mediaStore;
    this.eventHub = eventHub;
    this.logger = logger;
    this.reconnectDelayMs = reconnectDelayMs;
    this.sock = null;
    this.startPromise = null;
    this.connection = "idle";
    this.lastQr = null;
    this.lastQrAt = null;
    this.userJid = null;
    this.restartTimer = null;
  }

  async start() {
    if (this.startPromise) {
      return this.startPromise;
    }
    this.startPromise = this.#startOnce().finally(() => {
      this.startPromise = null;
    });
    return this.startPromise;
  }

  async #startOnce() {
    clearTimeout(this.restartTimer);
    const authDir = path.join(this.authRootDir, this.sessionId);
    await fs.mkdir(authDir, { recursive: true });
    const { state, saveCreds } = await useMultiFileAuthState(authDir);
    const logger = pino({ level: "silent" });
    const socketOptions = {
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, logger),
      },
      logger,
      printQRInTerminal: false,
      browser: ["Yuralume", "WhatsApp Sidecar", "0.1.0"],
      syncFullHistory: false,
      markOnlineOnConnect: false,
    };
    const version = await tryFetchLatestVersion(this.logger);
    if (version) {
      socketOptions.version = version;
    }
    this.sock = makeWASocket(socketOptions);
    this.connection = "connecting";
    this.sock.ev.on("creds.update", saveCreds);
    this.sock.ev.on("connection.update", (update) => this.#handleConnectionUpdate(update));
    this.sock.ev.on("messages.upsert", (upsert) => {
      void this.#handleMessagesUpsert(upsert);
    });
    this.sock.ws?.on?.("error", (error) => {
      this.logger.warn({ error: String(error), sessionId: this.sessionId }, "WhatsApp websocket error");
    });
  }

  status() {
    return {
      session_id: this.sessionId,
      connection: this.connection,
      qr_available: Boolean(this.lastQr),
      qr_updated_at: this.lastQrAt,
      user_jid: this.userJid,
    };
  }

  qr() {
    return {
      session_id: this.sessionId,
      qr: this.lastQr,
      updated_at: this.lastQrAt,
    };
  }

  async sendMessage({ chatRef, text, attachments }) {
    await this.start();
    if (!this.sock) {
      throw new Error("WhatsApp socket is not ready");
    }
    const jid = toWhatsAppJid(chatRef);
    const payloads = buildOutboundPayloads({ text, attachments });
    const messageIds = [];
    for (const payload of payloads) {
      const result = await this.sock.sendMessage(jid, payload);
      messageIds.push(result?.key?.id ?? "unknown");
    }
    return messageIds;
  }

  #handleConnectionUpdate(update) {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      this.lastQr = qr;
      this.lastQrAt = new Date().toISOString();
      this.logger.info({ sessionId: this.sessionId }, "WhatsApp QR updated; scan it with Linked Devices");
      qrcode.generate(qr, { small: true });
    }
    if (connection === "open") {
      this.connection = "open";
      this.lastQr = null;
      this.userJid = this.sock?.user?.id ?? null;
      this.logger.info({ sessionId: this.sessionId, userJid: this.userJid }, "WhatsApp connected");
    }
    if (connection === "close") {
      this.connection = "closed";
      const statusCode = statusCodeFromDisconnect(lastDisconnect?.error);
      this.logger.warn({ sessionId: this.sessionId, statusCode }, "WhatsApp connection closed");
      if (statusCode !== LOGGED_OUT_STATUS) {
        this.restartTimer = setTimeout(() => {
          void this.start();
        }, this.reconnectDelayMs);
      }
    }
  }

  async #handleMessagesUpsert(upsert) {
    const messages = Array.isArray(upsert?.messages) ? upsert.messages : [];
    for (const message of messages) {
      try {
        const mediaUrls = await this.#downloadMedia(message);
        const normalized = normalizeIncomingMessage(message, { mediaUrls });
        if (normalized) {
          this.eventHub.publish(this.sessionId, normalized);
        }
      } catch (error) {
        this.logger.warn({ error: String(error), sessionId: this.sessionId }, "Failed to normalize WhatsApp message");
      }
    }
  }

  async #downloadMedia(message) {
    const parts = extractMediaParts(message);
    if (parts.length === 0) {
      return [];
    }
    const urls = [];
    for (const part of parts) {
      try {
        const buffer = await downloadMediaMessage(
          message,
          "buffer",
          {},
          {
            logger: pino({ level: "silent" }),
            reuploadRequest: this.sock?.updateMediaMessage,
          },
        );
        const stored = await this.mediaStore.put({
          sessionId: this.sessionId,
          messageId: message?.key?.id,
          buffer,
          mimeType: part.mimeType,
        });
        urls.push(stored.url);
      } catch (error) {
        this.logger.warn({ error: String(error), sessionId: this.sessionId }, "Failed to download WhatsApp media");
      }
    }
    return urls;
  }
}

async function tryFetchLatestVersion(logger) {
  try {
    const result = await fetchLatestBaileysVersion();
    return result?.version;
  } catch (error) {
    logger.warn({ error: String(error) }, "Could not fetch latest Baileys version; using bundled default");
    return undefined;
  }
}

function statusCodeFromDisconnect(error) {
  return error?.output?.statusCode ?? error?.statusCode ?? error?.code;
}
