/**
 * Hosted runtime limits for the player surfaces — one shared snapshot.
 *
 * The hosted account profile caps character slots, creations per rolling 24h,
 * scenes per rolling 24h and messages per session, and switches album / TTS /
 * video generation on or off. All of it is enforced server-side; this module
 * exists so a surface can *warn before* the click rather than surface an
 * error after it.
 *
 * Module-scope refs make it a de-facto singleton, matching `useCloudCredits`
 * and `useActionPricing`: a sidebar, a create modal and a card panel all
 * showing the same ceiling cost exactly one request.
 *
 * ## Three rules this module exists to keep
 *
 * 1. **Advisory only — counters never disable.** A rolling counter (slots,
 *    creations, scene openings) must leave its control pressable: the count
 *    decays the moment it is read (24h windows refill continuously), so a
 *    disable would outlive its own truth with no path back — the refresh
 *    triggers all live behind the very button it turned off. The one narrow
 *    exception is a *capability switch* (album / TTS) that a loaded snapshot
 *    explicitly turns off: that is a static fact about the plan, the server
 *    will always refuse, and hiding/disabling spares a guaranteed error. A
 *    wrong "no" costs a player something they were entitled to, which is
 *    strictly worse than a redundant error message.
 * 2. **Unknown is never "blocked", and never "off".** A limit is only
 *    reported as reached when a real `used` count is at or over a real
 *    `limit`. Feature switches read `true` unless a loaded snapshot says
 *    `false` — a failed read must not take the album away.
 * 3. **Self-host renders nothing.** Non-cloud deployments issue zero requests
 *    and report `supported === false`, so every consumer's `v-if` collapses
 *    and the DOM is byte-identical to before this feature existed.
 *
 * Like `useCloudCredits`, reads carry the generation they were issued under:
 * a response that lands after the identity changed belongs to a player who is
 * no longer signed in and is dropped whole.
 */

import { computed, ref } from 'vue'

import { useAuth } from '@/composables/useAuth'
import {
  fetchRuntimeLimits,
  type RuntimeLimitUsage,
  type RuntimeLimitsSnapshot,
} from '@/utils/api/cloudLimits'
import { onIdentityChanged } from '@/utils/identityLifecycle'

/**
 * Why character creation would be refused, as far as the SPA can tell.
 * `slots_and_daily` exists because the two walls fail differently — deleting
 * a character frees a slot but does *not* refund the daily ledger — and a
 * player told only "say goodbye to make room" would pay an irreversible
 * deletion for a creation the server still refuses today.
 */
export type CharacterCreationBlock =
  | 'slots_full'
  | 'daily_exhausted'
  | 'slots_and_daily'

const snapshot = ref<RuntimeLimitsSnapshot | null>(null)
const loading = ref(false)
/** False once the backend answered 404 — this deployment has no hosted caps. */
const backendSupported = ref(true)
const loaded = ref(false)
let inFlight: Promise<void> | null = null
/** Bumped by `reset()`; a read from an older generation is discarded. */
let generation = 0
/**
 * Bumped per issued request. A forced `refresh()` supersedes whatever is in
 * flight — the older read was sent *before* the event that made the caller
 * refresh, so letting it stand would report a count from the past.
 */
let requestSeq = 0

/**
 * Cloud mode is read live rather than latched, because `useAuth` starts at
 * `self_host` and flips only after the auth config probe resolves. Latching
 * would make an early `ensureLoaded()` (a component mounting before the probe
 * lands) permanently mark a hosted deployment as limitless.
 */
function isCloud(): boolean {
  return useAuth().cloudMode.value
}

const supported = computed(() => isCloud() && backendSupported.value)

async function load(options: { force?: boolean } = {}): Promise<void> {
  // Self-host never asks: the route is not mounted there, and a 404 per page
  // load is noise that also teaches nothing.
  if (!isCloud()) return
  if (!backendSupported.value) return
  // Collapsing onto an in-flight read is only correct for ensureLoaded():
  // a forced refresh was triggered *by an event the in-flight read predates*,
  // so it must issue its own request and let the newest answer win.
  if (inFlight && !options.force) return inFlight
  const issuedAt = generation
  const seq = ++requestSeq
  loading.value = true
  const run = (async () => {
    try {
      const result = await fetchRuntimeLimits()
      if (issuedAt !== generation) return
      // A superseded read finishing late must not overwrite its successor.
      if (seq !== requestSeq) return
      if (result.kind === 'ok') {
        snapshot.value = result.snapshot
        loaded.value = true
      } else if (result.kind === 'unsupported') {
        backendSupported.value = false
        snapshot.value = null
        loaded.value = true
      }
      // 'degraded' deliberately falls through: keep whatever we had, and if
      // we had nothing, keep saying nothing. It also leaves `loaded` false —
      // one transient blip at page open must not silence every limit hint
      // for the whole session, so the next ensureLoaded() gets to retry.
    } finally {
      if (issuedAt === generation && seq === requestSeq) {
        loading.value = false
        inFlight = null
      }
    }
  })()
  inFlight = run
  return run
}

function ensureLoaded(): Promise<void> {
  if (loaded.value || loading.value) return inFlight ?? Promise.resolve()
  return load()
}

/**
 * Re-read after something that consumes a limit (a character created, a
 * scene opened). Always issues a fresh request — see `load()` on why it must
 * not collapse onto an in-flight read. Still one cheap call, still gated to
 * cloud mode.
 */
function refresh(): Promise<void> {
  return load({ force: true })
}

/**
 * Drop everything. `backendSupported` goes back to `true` on purpose: "no
 * hosted profile" was a fact about the previous operator, not this browser.
 */
function reset(): void {
  generation += 1
  snapshot.value = null
  backendSupported.value = true
  loaded.value = false
  loading.value = false
  inFlight = null
}

// Per-identity data: limits belong to the signed-in account, not the tab.
onIdentityChanged(reset)

/** A counter, or `null` when unsupported, unloaded, or genuinely uncapped. */
function usage(
  pick: (snap: RuntimeLimitsSnapshot) => RuntimeLimitUsage | null,
): RuntimeLimitUsage | null {
  if (!supported.value) return null
  const snap = snapshot.value
  return snap ? pick(snap) : null
}

/**
 * Feature switches default to enabled. Only a loaded snapshot that explicitly
 * says `false` turns one off — "we could not read the limits" must never
 * present as "this feature is not yours".
 */
function flag(pick: (snap: RuntimeLimitsSnapshot) => boolean): boolean {
  if (!supported.value) return true
  const snap = snapshot.value
  return snap ? pick(snap) : true
}

const characterSlots = computed(() => usage(snap => snap.character_slots))
const characterDailyCreates = computed(
  () => usage(snap => snap.character_daily_creates),
)
const storyScenesDaily = computed(() => usage(snap => snap.story_scenes_daily))

/**
 * Why a new character would be refused right now, or `null` when the SPA
 * cannot say so with certainty.
 *
 * "Certainty" is narrow by design: a known `used` at or over a known `limit`.
 * A `null` count (the backend served the ceiling but not the tally) is not
 * evidence of exhaustion. Both walls are reported when both are up
 * (`slots_and_daily`): deleting a character frees a slot but never refunds
 * the daily ledger, so "say goodbye to make room" alone would trade an
 * irreversible deletion for a creation the server still refuses today — the
 * everyday state of any tight profile (e.g. 1 slot with 1 create/day).
 */
const characterCreationBlocked = computed<CharacterCreationBlock | null>(() => {
  const slotsFull = reached(characterSlots.value)
  const dailySpent = reached(characterDailyCreates.value)
  if (slotsFull && dailySpent) return 'slots_and_daily'
  if (slotsFull) return 'slots_full'
  if (dailySpent) return 'daily_exhausted'
  return null
})

function reached(entry: RuntimeLimitUsage | null): boolean {
  return entry !== null && entry.used !== null && entry.used >= entry.limit
}

export function useRuntimeLimits() {
  return {
    // state
    /** False on self-host and on any operator without a hosted profile. */
    supported,
    loaded: computed(() => loaded.value),
    loading: computed(() => loading.value),

    // limits
    // (`session_message_limit` and `video_generation_enabled` stay in the
    // snapshot type but are deliberately not surfaced here: no player surface
    // consumes them yet, and an exported computed with zero callers reads as
    // a finished capability. Add the export together with its surface.)
    characterSlots,
    characterDailyCreates,
    storyScenesDaily,

    // feature switches (default enabled — unknown is never "off")
    albumGenerationEnabled: computed(
      () => flag(snap => snap.album_generation_enabled),
    ),
    ttsEnabled: computed(() => flag(snap => snap.tts_enabled)),

    // queries
    characterCreationBlocked,

    // actions
    ensureLoaded,
    refresh,
    reset,
  }
}
