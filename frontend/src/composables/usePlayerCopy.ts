/**
 * Reactive binding of the cloud-vs-self-host copy rule (plan U1).
 *
 * `pt()` ("player text") is a drop-in for `t()` at any surface that has a
 * hosted-player wording: it picks the `*Cloud` sibling when one exists and
 * we are hosted, and otherwise behaves exactly like `t()`. Self-host output
 * is therefore unchanged by construction — see `utils/playerCopy.ts` for the
 * rule itself and the reasoning behind the naming convention.
 */

import { useI18n } from 'vue-i18n'

import { useAuth } from '@/composables/useAuth'
import { CLOUD_COPY_SUFFIX, resolveCopyKey } from '@/utils/playerCopy'

export { CLOUD_COPY_SUFFIX, resolveCopyKey }

export type CopyParams = Record<string, unknown>

export function usePlayerCopy() {
  const { t, te } = useI18n()
  const { cloudMode } = useAuth()

  function keyFor(key: string): string {
    return resolveCopyKey(key, cloudMode.value, (candidate) => te(candidate))
  }

  function pt(key: string, params?: CopyParams): string {
    const resolved = keyFor(key)
    return params === undefined ? t(resolved) : t(resolved, params)
  }

  return { pt, keyFor, cloudMode }
}
