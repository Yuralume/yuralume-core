const UNSAFE_SESSION_CHARS = /[^A-Za-z0-9_.-]+/g;

export function normalizeSessionId(raw) {
  const value = String(raw ?? "").trim();
  if (!value) {
    throw new Error("session_id is required");
  }
  const normalized = value.replace(UNSAFE_SESSION_CHARS, "-").replace(/^-+|-+$/g, "");
  if (!normalized || normalized === "." || normalized === "..") {
    throw new Error("session_id is invalid");
  }
  return normalized;
}
