/**
 * Scheme guard for URLs rendered into an `<a :href>` (S2 defence in depth).
 *
 * Media URLs on characters/feeds/albums are server-owned object-storage URLs,
 * but a `.lumebackup` restore lands attacker-supplied rows — and the backend
 * scheme guard is the primary defence. This is the second layer: it keeps a
 * `javascript:` / `data:` / `vbscript:` URL from ever becoming a clickable
 * link that executes in our origin, no matter how it reached the client.
 *
 * A safe value (http/https, or a scheme-less relative/same-origin path) is
 * returned unchanged; anything else collapses to an empty string, which an
 * anchor renders as a no-op.
 */

/**
 * Drop the characters a browser removes while parsing a URL for navigation —
 * every C0 control (0x00–0x1F), space (0x20) and DEL (0x7F) — so the scheme
 * check sees what the browser would. Without this, `"java\tscript:alert(1)"`
 * reads as scheme-less and slips through.
 */
function canonicaliseForScheme(value: string): string {
  let out = ''
  for (const ch of value) {
    const code = ch.codePointAt(0) ?? 0
    if (code > 0x20 && code !== 0x7f) {
      out += ch
    }
  }
  return out.toLowerCase()
}

export function isSafeMediaUrl(value: unknown): boolean {
  if (typeof value !== 'string' || value === '') {
    return false
  }
  const canonical = canonicaliseForScheme(value)
  if (canonical === '') {
    return true
  }
  const schemeSep = canonical.indexOf(':')
  if (schemeSep === -1) {
    return true // no scheme → relative / same-origin
  }
  const slash = canonical.indexOf('/')
  if (slash !== -1 && slash < schemeSep) {
    return true // the ':' lives inside a path segment, not a scheme
  }
  const scheme = canonical.slice(0, schemeSep)
  return scheme === 'http' || scheme === 'https'
}

/** The `href`-safe form of a media URL: the value if safe, else `''`. */
export function safeMediaHref(value: unknown): string {
  return isSafeMediaUrl(value) ? (value as string) : ''
}
