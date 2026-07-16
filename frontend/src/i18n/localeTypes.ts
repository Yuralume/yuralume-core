/**
 * Supported locale registry — single source of truth for locale ids,
 * display labels, and the type that all locale catalogs must satisfy.
 *
 * Per docs/FRONTEND_I18N_PLAN.md:
 *  - `zh-TW` is the source locale and fallback. Its catalog defines the
 *    canonical shape every other locale must mirror.
 *  - `en-US` is the first translation target.
 *  - `ja-JP` is a first-class UI locale and player primary-language option.
 *  - BCP 47 ids; never bare "zh" or "en" to avoid ambiguity with future
 *    `zh-CN` / `en-GB` rollouts.
 *  - `user.primary_language` is reapplied by auth after login / token
 *    bootstrap as the player's default UI locale. UI locale still only
 *    drives chrome text + formatters, never LLM content language.
 */

export const SUPPORTED_LOCALES = ['zh-TW', 'en-US', 'ja-JP'] as const

export type SupportedLocale = (typeof SUPPORTED_LOCALES)[number]

/** Source locale — anchors fallback and the catalog shape. */
export const SOURCE_LOCALE: SupportedLocale = 'zh-TW'

/** Localised display names for each locale (shown in the switcher). */
export const LOCALE_LABELS: Record<SupportedLocale, string> = {
  'zh-TW': '繁體中文',
  'en-US': 'English',
  'ja-JP': '日本語',
}

/**
 * Narrow an unknown string to a supported locale, falling back to the
 * source locale on miss. Used by the localStorage / browser-language
 * detection paths so a stale value never crashes the runtime.
 */
export function coerceLocale(raw: string | null | undefined): SupportedLocale {
  if (!raw) return SOURCE_LOCALE
  return (SUPPORTED_LOCALES as readonly string[]).includes(raw)
    ? (raw as SupportedLocale)
    : SOURCE_LOCALE
}
