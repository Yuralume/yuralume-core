/**
 * Debounced city lookup for the hosted "where I live" picker (plan G2).
 *
 * Per-instance state (not a module singleton) — the onboarding gate and the
 * settings panel can both be alive at once and must not share a result list.
 *
 * Two properties the UI depends on:
 *   - keystrokes coalesce into one request after {@link CITY_SEARCH_DEBOUNCE_MS};
 *   - a superseded request can never overwrite a newer result, so a slow
 *     "ta" answer cannot clobber the "taipei" list the player is reading.
 */

import { ref } from 'vue'

import { searchPlaces } from '@/utils/api/playerLocale'
import type { GeoSearchResult } from '@/types/playerLocale'

/** Plan §4.2 asks for ≥300ms; 320 keeps a margin over timer coarseness. */
export const CITY_SEARCH_DEBOUNCE_MS = 320

/** One character matches half the planet — not worth a round trip. */
export const CITY_SEARCH_MIN_LENGTH = 2

export function useCitySearch() {
  const results = ref<GeoSearchResult[]>([])
  const searching = ref(false)
  /** True once a query has resolved, so "no matches" can be distinguished
   * from "nothing asked yet". */
  const searched = ref(false)

  let timer: ReturnType<typeof setTimeout> | null = null
  let sequence = 0

  function cancelPending(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
    // Invalidates any in-flight request as well: its ticket no longer
    // matches, so its response is dropped on arrival.
    sequence += 1
  }

  function reset(): void {
    cancelPending()
    results.value = []
    searching.value = false
    searched.value = false
  }

  function search(raw: string): void {
    cancelPending()
    const query = raw.trim()
    if (query.length < CITY_SEARCH_MIN_LENGTH) {
      results.value = []
      searching.value = false
      searched.value = false
      return
    }
    searching.value = true
    const ticket = sequence
    timer = setTimeout(() => {
      timer = null
      void run(query, ticket)
    }, CITY_SEARCH_DEBOUNCE_MS)
  }

  async function run(query: string, ticket: number): Promise<void> {
    // `searchPlaces` never rejects (fail-soft by contract), so no catch is
    // needed here — an upstream outage simply yields an empty list.
    const found = await searchPlaces(query)
    if (ticket !== sequence) return
    results.value = found
    searching.value = false
    searched.value = true
  }

  return { results, searching, searched, search, reset }
}
