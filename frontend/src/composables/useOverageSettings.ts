/**
 * Per-player quota-overage consent state (plan AP4).
 *
 * A module-scope singleton for the same reason `useCloudCredits` is one: this
 * is per-*identity* state, so it must be dropped from one place when the
 * signed-in player changes — a shared browser must never show one account's
 * spending consent to the next (see `@/utils/identityLifecycle`).
 *
 * `settings` stays `null` until a real read succeeds. Neither "unsupported"
 * (self-host / service off) nor "degraded" (momentarily unreadable) is allowed
 * to become a defaults object: a rendered switch is a statement about what the
 * player agreed to, and defaults would state the wrong thing.
 */

import { computed, ref } from 'vue'

import { isCloudDeployment } from '@/composables/deploymentMode'
import {
  fetchOverageSettings,
  updateOverageSettings,
  type OverageSettings,
} from '@/utils/api/overageSettings'
import { onIdentityChanged } from '@/utils/identityLifecycle'

export type OverageItem = 'chat_image' | 'feed_post'

const settings = ref<OverageSettings | null>(null)
const loading = ref(false)
const loaded = ref(false)
/** False once the backend answered 404 — nothing to consent to here. */
const supported = ref(true)
let inFlight: Promise<void> | null = null
let generation = 0

async function load(): Promise<void> {
  // There is no quota to overrun on self-host, so there is nothing to consent
  // to and no route to read. Only the read is gated — `setSwitch()` is a
  // deliberate player action and must keep failing loudly if it ever runs.
  if (!isCloudDeployment()) return
  if (!supported.value) return
  if (inFlight) return inFlight
  const issuedAt = generation
  loading.value = true
  inFlight = (async () => {
    try {
      const result = await fetchOverageSettings()
      if (issuedAt !== generation) return
      if (result.kind === 'ok') {
        settings.value = result.settings
      } else if (result.kind === 'unsupported') {
        supported.value = false
        settings.value = null
      }
      // 'degraded' keeps whatever was already known (usually nothing).
      loaded.value = true
    } finally {
      if (issuedAt === generation) {
        loading.value = false
        inFlight = null
      }
    }
  })()
  return inFlight
}

function ensureLoaded(): Promise<void> {
  if (loaded.value || loading.value) return inFlight ?? Promise.resolve()
  return load()
}

/**
 * Flip one switch and adopt the server's answer as the new truth.
 *
 * Throws on failure — deliberately, so the caller restores the control it
 * just moved. A consent switch that silently springs back with no explanation
 * is the one failure mode worse than an error line.
 */
async function setSwitch(item: OverageItem, value: boolean): Promise<void> {
  const issuedAt = generation
  const updated = await updateOverageSettings(
    item === 'chat_image'
      ? { chat_image_enabled: value }
      : { feed_post_enabled: value },
  )
  if (issuedAt !== generation) return
  settings.value = updated
}

function reset(): void {
  generation += 1
  settings.value = null
  supported.value = true
  loaded.value = false
  loading.value = false
  inFlight = null
}

onIdentityChanged(reset)

export function useOverageSettings() {
  return {
    settings: computed(() => settings.value),
    /** True only when real switch states are in hand. */
    ready: computed(() => settings.value !== null),
    loading: computed(() => loading.value),
    supported: computed(() => supported.value),
    ensureLoaded,
    reload: load,
    setSwitch,
    reset,
  }
}
