/**
 * Drafts behind the blocking first-login place / timezone / language
 * confirmation (plan G2 §4.2).
 *
 * Lives outside `FirstLoginLocaleGate.vue` for the same reason
 * `usePlayerLocaleSettings` does: the decisions that can quietly corrupt a
 * player's profile are payload-shaped, not pixel-shaped, and this repo has
 * no DOM test infrastructure. The two that matter here:
 *
 *   - **omission vs null.** Location keys are tri-state on the backend — an
 *     omitted key leaves the stored value alone, an explicit `null` clears
 *     it. A player who simply accepts the GeoIP guess must not send a
 *     location block at all, or accepting the guess would wipe it.
 *   - **the text and the coordinates are one fact.** Coordinates are
 *     invisible in this UI, so nothing on screen tells a player that the
 *     label they just typed over still carries the previous city's
 *     latitude. Editing the text therefore invalidates the whole set.
 */

import { ref } from 'vue'

import { useAuth } from '@/composables/useAuth'
import { useTimezone } from '@/composables/useTimezone'
import type { GeoSearchResult, LocaleConfirmRequest } from '@/types/playerLocale'

export function useFirstLoginLocaleDraft() {
  const { currentUser, locationHint } = useAuth()
  const { browserTimezone } = useTimezone()

  const placeQuery = ref('')
  const timezoneId = ref('')
  const primaryLanguage = ref('')
  const countryCode = ref<string | null>(null)
  const latitude = ref<number | null>(null)
  const longitude = ref<number | null>(null)
  /** True while the label is backed by real coordinates worth sending. */
  const placePicked = ref(false)
  const seededLabel = ref('')
  /**
   * What the last accepted suggestion wrote into the timezone field, and
   * what stood there before. Invalidating the place undoes that write — but
   * only if the player has not since chosen a timezone of their own, which
   * is theirs to keep regardless of which city the text now names.
   */
  const timezoneFromPick = ref<{ applied: string; previous: string } | null>(null)

  /**
   * Seed from the strongest available guess: a fresh login hint beats the
   * stored profile, which beats the browser's own timezone. Everything shown
   * is a *proposal* the player accepts or overwrites.
   */
  function seed(): void {
    const user = currentUser.value
    const hint = locationHint.value
    seededLabel.value = hint?.label ?? user?.location_label ?? ''
    placeQuery.value = seededLabel.value
    timezoneId.value = hint?.timezone_id || user?.timezone_id || browserTimezone()
    primaryLanguage.value = user?.primary_language ?? ''
    countryCode.value = hint?.country_code ?? user?.country_code ?? null
    latitude.value = hint?.latitude ?? user?.latitude ?? null
    longitude.value = hint?.longitude ?? user?.longitude ?? null
    placePicked.value = Boolean(hint)
    timezoneFromPick.value = null
  }

  function onPlaceSelected(place: GeoSearchResult): void {
    // The field's `v-model` has already written this; repeating it keeps the
    // label and its coordinates a single assignment rather than two emits
    // whose order the payload silently depends on.
    placeQuery.value = place.label
    placePicked.value = true
    countryCode.value = place.country_code
    latitude.value = place.latitude
    longitude.value = place.longitude
    if (place.timezone_id && place.timezone_id !== timezoneId.value) {
      timezoneFromPick.value = {
        applied: place.timezone_id,
        previous: timezoneId.value,
      }
      timezoneId.value = place.timezone_id
    }
  }

  /**
   * The text stopped standing for a place. Everything the place contributed
   * goes with it — coordinates and country outright, and the timezone only
   * if it is still exactly what the pick put there.
   */
  function onPlaceCleared(): void {
    placePicked.value = false
    countryCode.value = null
    latitude.value = null
    longitude.value = null
    const pick = timezoneFromPick.value
    if (pick && timezoneId.value === pick.applied) timezoneId.value = pick.previous
    timezoneFromPick.value = null
  }

  /**
   * An untouched place must not appear in the body at all (see the
   * omission-vs-null note above). A hand-typed label is sent as a label and
   * nothing else: it is a name the player wrote, not a located place, and
   * pairing it with whatever coordinates happened to be in memory is how
   * a profile ends up claiming Tokyo sits in Taipei.
   */
  function confirmPayload(): LocaleConfirmRequest {
    const payload: LocaleConfirmRequest = { dismiss_location_hint: true }
    if (timezoneId.value.trim()) payload.timezone_id = timezoneId.value.trim()
    if (primaryLanguage.value.trim()) {
      payload.primary_language = primaryLanguage.value.trim()
    }
    const label = placeQuery.value.trim()
    if (placePicked.value) {
      payload.country_code = countryCode.value
      payload.latitude = latitude.value
      payload.longitude = longitude.value
      payload.location_label = label || null
    } else if (label !== seededLabel.value) {
      payload.country_code = null
      payload.latitude = null
      payload.longitude = null
      payload.location_label = label || null
    }
    return payload
  }

  return {
    placeQuery,
    timezoneId,
    primaryLanguage,
    placePicked,
    seed,
    onPlaceSelected,
    onPlaceCleared,
    confirmPayload,
  }
}
