/**
 * Reactive accessor for the UI locale.
 *
 * Wraps `i18n.global.locale` (the vue-i18n composer's locale ref) and
 * pairs it with side effects:
 *   - write to localStorage so the choice survives reload
 *   - sync `document.documentElement.lang` so screen readers / native
 *     form widgets pick up the language
 *
 * `user.primary_language` is the authenticated user's default UI
 * language. The auth flow reapplies it whenever login / token bootstrap
 * resolves a user, while the switcher can still change the chrome during
 * the current authenticated session.
 */

import { computed } from 'vue'

import { LOCALE_STORAGE_KEY, i18n } from '@/i18n'
import {
  LOCALE_LABELS,
  SOURCE_LOCALE,
  SUPPORTED_LOCALES,
  type SupportedLocale,
  coerceLocale,
} from '@/i18n/localeTypes'

function writeStorage(locale: SupportedLocale): void {
  if (typeof window === 'undefined') return
  try {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, locale)
  } catch {
    // localStorage denied — UI still works, just won't survive reload.
  }
}

function clearStorageIfMatches(locale: SupportedLocale): void {
  if (typeof window === 'undefined') return
  try {
    if (window.localStorage.getItem(LOCALE_STORAGE_KEY) === locale) {
      window.localStorage.removeItem(LOCALE_STORAGE_KEY)
    }
  } catch {
    /* noop */
  }
}

function syncDocumentLang(locale: SupportedLocale): void {
  if (typeof document === 'undefined') return
  document.documentElement.lang = locale
}

// Bootstrap `<html lang>` on first import so the initial render
// matches the runtime locale.
syncDocumentLang(coerceLocale(String(i18n.global.locale.value)))

export function useLocale() {
  const locale = computed<SupportedLocale>({
    get: () => coerceLocale(String(i18n.global.locale.value)),
    set: (next) => {
      const coerced = coerceLocale(next)
      i18n.global.locale.value = coerced
      writeStorage(coerced)
      syncDocumentLang(coerced)
    },
  })

  /**
   * Authenticated identity wins over stale browser storage. This keeps
   * shared browsers from showing the previous player's UI language after
   * a different account logs in.
   */
  function applyPrimaryLanguage(primaryLanguage: string): void {
    const coerced = coerceLocale(primaryLanguage)
    i18n.global.locale.value = coerced
    writeStorage(coerced)
    syncDocumentLang(coerced)
  }

  /**
   * Forget the persisted UI locale so the next mount falls back to
   * browser language / primary language detection. Useful for logout.
   */
  function resetToFallback(): void {
    if (typeof window !== 'undefined') {
      try {
        window.localStorage.removeItem(LOCALE_STORAGE_KEY)
      } catch {
        /* noop */
      }
    }
    i18n.global.locale.value = SOURCE_LOCALE
    syncDocumentLang(SOURCE_LOCALE)
  }

  const supported = computed(() =>
    SUPPORTED_LOCALES.map((code) => ({
      code,
      label: LOCALE_LABELS[code],
    })),
  )

  return {
    locale,
    supported,
    applyPrimaryLanguage,
    resetToFallback,
    clearStorageIfMatches,
  }
}
