import path from "node:path";

const DEFAULT_PORT = 32190;

export function loadConfig(env = process.env) {
  const port = parsePort(env.WHATSAPP_SIDECAR_PORT, DEFAULT_PORT);
  const host = nonEmpty(env.WHATSAPP_SIDECAR_HOST) ?? "0.0.0.0";
  const authDir = nonEmpty(env.WHATSAPP_SIDECAR_AUTH_DIR) ?? "/data/auth";
  const mediaDir = nonEmpty(env.WHATSAPP_SIDECAR_MEDIA_DIR) ?? "/data/media";
  const baseUrl =
    trimTrailingSlash(nonEmpty(env.WHATSAPP_SIDECAR_BASE_URL))
    ?? `http://whatsapp-sidecar:${port}`;
  return {
    host,
    port,
    authDir: path.resolve(authDir),
    mediaDir: path.resolve(mediaDir),
    baseUrl,
    apiToken: nonEmpty(env.WHATSAPP_SIDECAR_API_TOKEN) ?? "",
    logLevel: nonEmpty(env.WHATSAPP_SIDECAR_LOG_LEVEL) ?? "info",
    maxEventHistory: parsePositiveInt(env.WHATSAPP_SIDECAR_EVENT_HISTORY, 100),
    sseHeartbeatMs: parsePositiveInt(env.WHATSAPP_SIDECAR_SSE_HEARTBEAT_MS, 15000),
    reconnectDelayMs: parsePositiveInt(env.WHATSAPP_SIDECAR_RECONNECT_DELAY_MS, 5000),
  };
}

function nonEmpty(value) {
  if (typeof value !== "string") {
    return undefined;
  }
  const trimmed = value.trim();
  return trimmed ? trimmed : undefined;
}

function trimTrailingSlash(value) {
  return value?.replace(/\/+$/, "");
}

function parsePort(raw, fallback) {
  const parsed = Number.parseInt(raw ?? "", 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 65535) {
    return fallback;
  }
  return parsed;
}

function parsePositiveInt(raw, fallback) {
  const parsed = Number.parseInt(raw ?? "", 10);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    return fallback;
  }
  return parsed;
}
