/**
 * vue-i18n runtime.
 *
 * Why composition-mode (`legacy: false`): `<script setup>` reads `t()`
 * via `useI18n()`; legacy mode would force every component to live on
 * the options API. This module owns UI chrome locale; auth later
 * re-applies the authenticated user's `primary_language` after login or
 * stored-token bootstrap.
 *
 * The runtime locale is bootstrapped from `detectInitialLocale()` here
 * so the chrome renders correctly on the very first paint; `useLocale`
 * later wraps `i18n.global.locale` for reactive get/set + side effects
 * (localStorage write, `document.documentElement.lang`).
 */

import { createI18n } from 'vue-i18n'

import {
  SOURCE_LOCALE,
  SUPPORTED_LOCALES,
  type SupportedLocale,
  coerceLocale,
} from './localeTypes'
import { messages as zhTW } from './locales/zh-TW'
import { messages as enUS } from './locales/en-US'
import { messages as jaJP } from './locales/ja-JP'

export const LOCALE_STORAGE_KEY = 'kokoro.locale'

/**
 * Decide which locale to start in before auth is known:
 *   localStorage > browser language > zh-TW
 *
 * `user.primary_language` is intentionally NOT consulted here — `main.ts`
 * doesn't have `/auth/me` yet at module evaluation time. `useLocale`
 * exposes `applyPrimaryLanguage()` so the auth flow can call it after
 * `/auth/me` resolves and replace any stale browser-stored locale with
 * the authenticated player's primary language.
 */
function detectInitialLocale(): SupportedLocale {
  if (typeof window === 'undefined') {
    return SOURCE_LOCALE
  }
  try {
    const stored = window.localStorage.getItem(LOCALE_STORAGE_KEY)
    if (stored) {
      return coerceLocale(stored)
    }
  } catch {
    // localStorage may be denied (private mode). Fall through.
  }
  const browser = window.navigator?.language ?? ''
  if (browser) {
    // Direct match first (`zh-TW`, `en-US`).
    if ((SUPPORTED_LOCALES as readonly string[]).includes(browser)) {
      return browser as SupportedLocale
    }
    // Fuzzy match on language subtag: `zh-Hant-TW` → `zh-TW`,
    // `en-GB` → `en-US`, etc. Keep this loose so users on locales we
    // don't fully support still land somewhere reasonable.
    const head = browser.split('-')[0]?.toLowerCase()
    if (head === 'zh') return 'zh-TW'
    if (head === 'en') return 'en-US'
    if (head === 'ja') return 'ja-JP'
  }
  return SOURCE_LOCALE
}

/**
 * Whether we're in a dev build. The i18n audit is done, so misses are
 * now loud in dev to catch "referenced key not in catalogue" races
 * before they ship (a miss silently falls back to zh-TW, which reads
 * fine to zh operators but is invisible breakage for en/ja). Production
 * stays quiet — a shipped miss shouldn't spam the user's console.
 */
const IS_DEV = Boolean(import.meta.env?.DEV)

export const i18n = createI18n({
  legacy: false,
  locale: detectInitialLocale(),
  // ja falls back through en-US before the zh-TW ship-first source, so a
  // Japanese self-hoster sees English rather than Chinese on any miss.
  fallbackLocale: {
    'ja-JP': ['en-US', SOURCE_LOCALE],
    default: [SOURCE_LOCALE],
  },
  // Loud in dev (audit complete), quiet in production.
  missingWarn: IS_DEV,
  fallbackWarn: IS_DEV,
  missing: IS_DEV
    ? (locale, key) => {
        // Dev-only visibility: a key referenced by a component but
        // absent from the catalogue silently degrades to the fallback
        // locale, so surface it here instead of letting it hide.
        // eslint-disable-next-line no-console
        console.warn(`[i18n] missing key "${key}" for locale "${locale}"`)
      }
    : undefined,
  messages: {
    'zh-TW': zhTW,
    'en-US': enUS,
    'ja-JP': jaJP,
  },
})

export {
  SOURCE_LOCALE,
  SUPPORTED_LOCALES,
  LOCALE_LABELS,
  coerceLocale,
  type SupportedLocale,
} from './localeTypes'
