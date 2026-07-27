/**
 * The hosted-player jargon denylist (plan U1-F).
 *
 * Hosted Yuralume is aimed at players who never asked what a webhook is.
 * This fixture is the machine-readable half of that promise: every term
 * here is something a *player* cannot act on, so it must not survive into
 * cloud-mode rendering. Self-host is deliberately untouched — an operator
 * wants to read "ComfyUI" and "YAML", they are actionable leads there.
 *
 * Adding a term here retroactively guards every scanned surface, which is
 * the point: new copy is checked without anyone remembering to check it.
 */

/**
 * Terms banned from hosted player surfaces.
 *
 * Matched case-insensitively as substrings, so `LoRA` also catches
 * `lora` and `Prompt` catches `prompt`. Kept as plain strings rather
 * than regexes so the list stays readable and diffable.
 */
export const CLOUD_JARGON_TERMS: readonly string[] = [
  // Generation / model plumbing
  'ComfyUI',
  'LoRA',
  'LLM',
  'SoVITS',
  'embedding',
  'prompt',
  'provider',
  'API key',
  // Transport / integration plumbing
  'webhook',
  'endpoint',
  'VAPID',
  // Config / ops plumbing
  'YAML',
  'token',
  'fallback',
  'self-host',
  '資料庫',
  '部署',
  '環境變數',
]

/**
 * Terms that are *not* jargon even though they look technical.
 *
 * `/pic` is the player's own command, and the platform names are what
 * the player reads on the box. Listed for the reader's benefit: nothing
 * in `CLOUD_JARGON_TERMS` matches them, and this array documents that
 * absence so nobody "fixes" it later.
 */
export const NON_JARGON_PRODUCT_TERMS: readonly string[] = [
  '/pic',
  'LINE',
  'Discord',
  'WhatsApp',
  'Telegram',
  'Yuralume',
]

/**
 * Per-surface exemptions.
 *
 * Keyed by the scan name used in the test, never global: an exemption is
 * a statement about one screen, so a leak on any other screen still
 * fails. The external-channel surfaces are the single carve-out owner
 * signed off on (plan §8-6): binding your own bot means reading your own
 * bot's credentials, and those words — Bot token, Channel secret,
 * webhook URL — are the platform's own labels. Translating them would
 * leave the player unable to find the field in Discord's console.
 */
export const COMPONENT_JARGON_ALLOWLIST: Readonly<
  Record<string, readonly string[]>
> = {
  ChannelBindingsPanel: ['token', 'webhook', 'secret', 'endpoint'],
  ChannelSetupGuide: ['token', 'webhook', 'secret', 'endpoint'],
  ChannelAccountNextStep: ['token', 'webhook', 'secret', 'endpoint'],
}

/**
 * i18n key prefixes carrying the same carve-out, for the catalogue scan.
 *
 * The catalogue pass covers hosted copy that no test currently renders
 * (a create form behind a click, an error branch behind a failure), so
 * it needs the exemptions expressed against keys rather than components.
 */
export const CATALOG_JARGON_ALLOWLIST: Readonly<
  Record<string, readonly string[]>
> = {
  'channelBindingsPanel.': ['token', 'webhook', 'secret', 'endpoint'],
  'channelSetupGuide.': ['token', 'webhook', 'secret', 'endpoint'],
  'channelAccountNextStep.': ['token', 'webhook', 'secret', 'endpoint'],
}

function escapeForRegExp(term: string): string {
  return term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * Every denied term present in `text`, minus the ones `allowed` exempts.
 *
 * Returns the matches rather than a boolean so a failing assertion names
 * the offending word instead of just saying "somewhere in 4 KB of HTML".
 */
export function findJargon(
  text: string,
  allowed: readonly string[] = [],
): string[] {
  const exempt = new Set(allowed.map(term => term.toLowerCase()))
  return CLOUD_JARGON_TERMS.filter((term) => {
    if (exempt.has(term.toLowerCase())) return false
    return new RegExp(escapeForRegExp(term), 'i').test(text)
  })
}

/** Exemptions for a catalogue key, resolved by longest matching prefix. */
export function allowlistForKey(key: string): readonly string[] {
  let best: readonly string[] = []
  let bestLength = -1
  for (const [prefix, terms] of Object.entries(CATALOG_JARGON_ALLOWLIST)) {
    if (key.startsWith(prefix) && prefix.length > bestLength) {
      best = terms
      bestLength = prefix.length
    }
  }
  return best
}
