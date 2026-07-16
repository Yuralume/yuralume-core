/**
 * Single source of truth for "the operator's language" used when
 * translating shipped/authored content for display.
 *
 * Resolves to the operator's **stored primary language** (from
 * `/auth/me`, mirrored in `useAuth().currentUser`) and only falls back
 * to the current UI locale when that is unavailable. This matches the
 * bind/materialise path (`story_arc_service._resolve_operator_language`)
 * and the preview endpoint's server-side fallback, so a preview shows
 * the same language the operator will actually read once an arc is
 * materialised — "what you preview is what you get" (plan P4).
 *
 * The UI locale is only a reading-chrome preference; primary_language is
 * the identity-level "language characters speak to me in", so it is the
 * authoritative translation target.
 */

import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'

export function useOperatorLanguage() {
  const { locale } = useI18n()
  const { currentUser } = useAuth()

  const language = computed<string>(() => {
    const primary = currentUser.value?.primary_language
    if (primary && primary.trim()) return primary.trim()
    return String(locale.value)
  })

  return {
    /** Reactive operator language tag (e.g. `zh-TW` / `en-US` / `ja-JP`). */
    language,
    /** Getter form for injection into `useArcTemplateTranslation`. */
    targetLanguage: () => language.value,
  }
}
