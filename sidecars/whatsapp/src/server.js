import fs from "node:fs";
import http from "node:http";
import QRCode from "qrcode";
import { requireAuthorized } from "./auth.js";
import { normalizeSessionId } from "./sessionId.js";

export function createServer({ config, sessionManager, eventHub, mediaStore, logger }) {
  return http.createServer(async (request, response) => {
    try {
      await routeRequest({ request, response, config, sessionManager, eventHub, mediaStore, logger });
    } catch (error) {
      logger.error({ error: String(error) }, "Unhandled sidecar request error");
      sendJson(response, 500, { ok: false, error: "internal_error" });
    }
  });
}

async function routeRequest(params) {
  const { request, response, config, sessionManager, eventHub, mediaStore } = params;
  const url = new URL(request.url ?? "/", `http://${request.headers.host ?? "localhost"}`);
  const segments = url.pathname.split("/").filter(Boolean).map(decodeURIComponent);

  if (request.method === "GET" && url.pathname === "/health") {
    sendJson(response, 200, { ok: true });
    return;
  }

  if (segments[0] !== "sessions" || !segments[1]) {
    sendJson(response, 404, { ok: false, error: "not_found" });
    return;
  }

  const sessionId = normalizeSessionId(segments[1]);

  if (request.method === "GET" && segments[2] === "media" && segments[3]) {
    await serveMedia({ response, mediaStore, sessionId, filename: segments[3] });
    return;
  }

  if (!requireAuthorized(request, response, config.apiToken)) {
    return;
  }

  if (request.method === "GET" && segments[2] === "status") {
    const session = await sessionManager.start(sessionId);
    sendJson(response, 200, { ok: true, ...session.status() });
    return;
  }

  if (request.method === "GET" && segments[2] === "qr") {
    const session = await sessionManager.start(sessionId);
    sendJson(response, 200, { ok: true, ...session.qr() });
    return;
  }

  if (request.method === "GET" && segments[2] === "qr.svg") {
    const session = await sessionManager.start(sessionId);
    await sendQrSvg(response, session.qr().qr);
    return;
  }

  if (request.method === "GET" && segments[2] === "events") {
    await sessionManager.start(sessionId);
    streamEvents({ request, response, eventHub, sessionId, heartbeatMs: config.sseHeartbeatMs });
    return;
  }

  if (request.method === "POST" && segments[2] === "messages") {
    const body = await readJsonBody(request).catch((error) => {
      if (error instanceof HttpRequestError) {
        sendJson(response, error.status, { ok: false, error: error.code });
        return null;
      }
      throw error;
    });
    if (body === null) {
      return;
    }
    const session = await sessionManager.start(sessionId);
    const messageIds = await session.sendMessage({
      chatRef: body.chat_ref,
      text: body.text ?? "",
      attachments: body.attachments ?? [],
    });
    sendJson(response, 200, { ok: true, message_ids: messageIds });
    return;
  }

  sendJson(response, 404, { ok: false, error: "not_found" });
}

function streamEvents({ request, response, eventHub, sessionId, heartbeatMs }) {
  response.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-store, no-transform",
    connection: "keep-alive",
    "x-accel-buffering": "no",
  });
  response.write(": connected\n\n");
  const unsubscribe = eventHub.subscribe(
    sessionId,
    (event) => writeSseEvent(response, "message", event),
    { replay: false },
  );
  const heartbeat = setInterval(() => {
    response.write(": ping\n\n");
  }, heartbeatMs);
  const cleanup = () => {
    clearInterval(heartbeat);
    unsubscribe();
    response.end();
  };
  request.on("close", cleanup);
}

async function serveMedia({ response, mediaStore, sessionId, filename }) {
  let resolved;
  try {
    resolved = mediaStore.resolve({ sessionId, filename });
  } catch {
    sendJson(response, 400, { ok: false, error: "bad_media_path" });
    return;
  }
  if (!fs.existsSync(resolved)) {
    sendJson(response, 404, { ok: false, error: "media_not_found" });
    return;
  }
  response.writeHead(200, {
    "content-type": contentTypeForFilename(filename),
    "cache-control": "private, max-age=86400",
  });
  fs.createReadStream(resolved).pipe(response);
}

function sendJson(response, status, payload) {
  if (response.headersSent) {
    response.end();
    return;
  }
  response.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(JSON.stringify(payload));
}

function writeSseEvent(response, eventName, payload) {
  response.write(`event: ${eventName}\n`);
  response.write(`data: ${JSON.stringify(payload)}\n\n`);
}

async function sendQrSvg(response, qr) {
  if (!qr) {
    sendJson(response, 404, { ok: false, error: "qr_not_available" });
    return;
  }
  const svg = await QRCode.toString(qr, {
    type: "svg",
    margin: 1,
    width: 240,
    errorCorrectionLevel: "M",
  });
  response.writeHead(200, {
    "content-type": "image/svg+xml; charset=utf-8",
    "cache-control": "no-store",
  });
  response.end(svg);
}

async function readJsonBody(request) {
  return new Promise((resolve, reject) => {
    let body = "";
    let settled = false;
    const settle = (fn, value) => {
      if (settled) {
        return;
      }
      settled = true;
      fn(value);
    };
    request.setEncoding("utf8");
    request.on("data", (chunk) => {
      body += chunk;
      if (body.length > 1_000_000) {
        request.pause();
        settle(reject, new HttpRequestError(413, "request_body_too_large"));
      }
    });
    request.on("error", (error) => {
      settle(reject, error);
    });
    request.on("end", () => {
      if (settled) {
        return;
      }
      if (!body.trim()) {
        settle(resolve, {});
        return;
      }
      try {
        settle(resolve, JSON.parse(body));
      } catch {
        settle(reject, new HttpRequestError(400, "request_body_invalid_json"));
      }
    });
  });
}

class HttpRequestError extends Error {
  constructor(status, code) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

function contentTypeForFilename(filename) {
  const lower = filename.toLowerCase();
  if (lower.endsWith(".jpg") || lower.endsWith(".jpeg")) return "image/jpeg";
  if (lower.endsWith(".png")) return "image/png";
  if (lower.endsWith(".webp")) return "image/webp";
  if (lower.endsWith(".gif")) return "image/gif";
  if (lower.endsWith(".mp4")) return "video/mp4";
  if (lower.endsWith(".mp3")) return "audio/mpeg";
  if (lower.endsWith(".ogg") || lower.endsWith(".opus")) return "audio/ogg";
  if (lower.endsWith(".pdf")) return "application/pdf";
  return "application/octet-stream";
}
