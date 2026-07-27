/**
 * Drafts + effects behind the personal "where I live / timezone / language"
 * block (plan G2 §4.2).
 *
 * Lives outside the component so the three decisions that can actually harm
 * a player are unit-testable without a DOM:
 *
 *   - a no-op save must never spend one of the two changes per 30 days;
 *   - a change must never happen before the irreversibility warning is
 *     acknowledged;
 *   - accepting or ignoring a relocation hint must not cost a change either
 *     (both ride the free, idempotent confirm endpoint or the plain profile
 *     write).
 *
 * The i18n and confirm-dialog seams are injected rather than imported so a
 * test can assert on message keys and on *whether the dialog was shown*.
 */

import { computed, ref } from 'vue'

import { useAuth } from '@/composables/useAuth'
import type { OperatorProfile } from '@/types/operator'
import type { GeoSearchResult } from '@/types/playerLocale'
import { updateOperatorProfile } from '@/utils/api/operatorProfile'
import {
  LOCALE_CHANGE_LIMIT,
  LOCALE_CHANGE_WINDOW_DAYS,
  LocaleChangeInProgressError,
  LocaleChangeLimitError,
  changeLocale,
  confirmLocale,
} from '@/utils/api/playerLocale'
import { normalizeCoordinateInput } from '@/utils/coordinateInput'
import {
  localeChangePayload,
  localeChangeSubject,
  type LocaleChangeSubject,
} from '@/utils/localeLifecycle'

export interface LocaleSettingsDeps {
  translate: (key: string, params?: Record<string, unknown>) => string
  confirm: (options: {
    title: string
    content: string
    okText: string
  }) => Promise<boolean>
  /** Renders an ISO instant as user-local civil time. */
  formatRetryAt: (iso: string) => string
}

const PROFILE_UPDATED_EVENT = 'kokoro:operator-profile-updated'

export function usePlayerLocaleSettings(deps: LocaleSettingsDeps) {
  const { cloudMode, currentUser, locationHint, refreshMe } = useAuth()

  const countryCodeDraft = ref('')
  // Vue auto-casts v-model on <input type="number"> to a JS number even
  // without the .number modifier, so these can hold a number after manual
  // entry despite being seeded with ''. See normalizeCoordinateInput.
  const latitudeDraft = ref<string | number>('')
  const longitudeDraft = ref<string | number>('')
  const locationLabelDraft = ref('')
  const locationSaving = ref(false)
  const locationFeedback = ref<string | null>(null)

  const timezoneDraft = ref('')
  const languageDraft = ref('')
  const localeSaving = ref(false)
  const localeFeedback = ref<string | null>(null)
  const hintBusy = ref(false)

  /**
   * Only known once a locale endpoint has answered. Until then the UI shows
   * the static guardrail wording rather than POSTing on page load just to
   * read a counter.
   */
  const remainingChanges = ref<number | null>(null)
  const changeLimit = ref(LOCALE_CHANGE_LIMIT)
  const changeWindowDays = ref(LOCALE_CHANGE_WINDOW_DAYS)

  const localeLimitHint = computed(() => {
    if (remainingChanges.value === null) {
      return deps.translate('playerLocale.change.limitHint', {
        limit: changeLimit.value,
        days: changeWindowDays.value,
      })
    }
    if (remainingChanges.value <= 0) {
      return deps.translate('playerLocale.change.exhausted', {
        days: changeWindowDays.value,
      })
    }
    return deps.translate('playerLocale.change.remaining', {
      count: remainingChanges.value,
      days: changeWindowDays.value,
    })
  })

  function seedFromProfile(profile: OperatorProfile | null): void {
    countryCodeDraft.value = profile?.country_code ?? ''
    latitudeDraft.value = profile?.latitude == null ? '' : String(profile.latitude)
    longitudeDraft.value = profile?.longitude == null ? '' : String(profile.longitude)
    locationLabelDraft.value = profile?.location_label ?? ''
    // Identity locale follows the auth payload, not the operator profile: it
    // is the one re-read after a change, so seeding from the (still stale)
    // profile would bounce the drafts back.
    timezoneDraft.value = currentUser.value?.timezone_id ?? profile?.timezone_id ?? ''
    languageDraft.value = currentUser.value?.primary_language ?? ''
  }

  /**
   * The per-player write claim was held by another in-flight write. Nothing
   * the player chose is wrong and no budget was touched, so the message asks
   * for a retry rather than reporting a failure — and the remaining-changes
   * counter is deliberately left untouched, because nothing was spent.
   */
  function failureMessage(fallbackKey: string, err: unknown): string {
    if (err instanceof LocaleChangeInProgressError) {
      return deps.translate('playerLocale.change.inProgress')
    }
    return err instanceof Error
      ? deps.translate('common.errorWithDetail', {
        message: deps.translate(fallbackKey),
        detail: err.message,
      })
      : deps.translate(fallbackKey)
  }

  function locationPayload() {
    const country = countryCodeDraft.value.trim()
    const label = locationLabelDraft.value.trim()
    return {
      country_code: country || null,
      latitude: normalizeCoordinateInput(latitudeDraft.value),
      longitude: normalizeCoordinateInput(longitudeDraft.value),
      location_label: label || null,
    }
  }

  function publish(profile: OperatorProfile): void {
    if (typeof window === 'undefined') return
    window.dispatchEvent(new CustomEvent(PROFILE_UPDATED_EVENT, {
      detail: profile,
    }))
  }

  async function saveLocation(): Promise<void> {
    if (!currentUser.value) return
    locationSaving.value = true
    locationFeedback.value = null
    try {
      const profile = await updateOperatorProfile(locationPayload())
      seedFromProfile(profile)
      publish(profile)
      locationFeedback.value = profile.location_label || profile.country_code
        ? deps.translate('playerSidebar.location.saved')
        : deps.translate('playerSidebar.location.cleared')
      // A stored country that now matches the hint retires it server-side,
      // so re-read rather than leaving a stale suggestion on screen.
      if (cloudMode.value && locationHint.value) await refreshMe()
    } catch (err) {
      locationFeedback.value = failureMessage(
        'playerSidebar.location.saveFailed', err,
      )
    } finally {
      locationSaving.value = false
    }
  }

  async function clearLocation(): Promise<void> {
    countryCodeDraft.value = ''
    latitudeDraft.value = ''
    longitudeDraft.value = ''
    locationLabelDraft.value = ''
    await saveLocation()
  }

  function onPlaceSelected(place: GeoSearchResult): void {
    locationLabelDraft.value = place.label
    latitudeDraft.value = place.latitude
    longitudeDraft.value = place.longitude
    countryCodeDraft.value = place.country_code ?? ''
    locationFeedback.value = null
  }

  /**
   * The label stopped standing for a picked city — hand-typed, or typed over
   * a suggestion. The coordinates and country go with it: they are invisible
   * in the cloud branch, so keeping them would silently save "the name I
   * typed" at "the last city I clicked". A bare label is still saved, just
   * as a name and nothing more.
   */
  function onPlaceCleared(): void {
    latitudeDraft.value = ''
    longitudeDraft.value = ''
    countryCodeDraft.value = ''
    locationFeedback.value = null
  }

  /**
   * Optional shortcut. A refusal is not an error state — the city search is
   * the primary path and stays right where it was.
   */
  function useCurrentLocation(): void {
    const geolocation = typeof navigator === 'undefined'
      ? undefined
      : navigator.geolocation
    if (!geolocation) {
      locationFeedback.value = deps.translate(
        'playerLocale.location.useCurrentUnsupported',
      )
      return
    }
    geolocation.getCurrentPosition(
      (position) => {
        latitudeDraft.value = position.coords.latitude
        longitudeDraft.value = position.coords.longitude
        locationFeedback.value = deps.translate(
          'playerLocale.location.useCurrentDone',
        )
      },
      () => {
        locationFeedback.value = deps.translate(
          'playerLocale.location.useCurrentDenied',
        )
      },
      { timeout: 10000, maximumAge: 60000 },
    )
  }

  function subjectLabel(subject: LocaleChangeSubject): string {
    if (subject === 'timezone') {
      return deps.translate('playerLocale.change.subjectTimezone')
    }
    if (subject === 'language') {
      return deps.translate('playerLocale.change.subjectLanguage')
    }
    return deps.translate('playerLocale.change.subjectBoth')
  }

  function limitMessage(error: LocaleChangeLimitError): string {
    changeLimit.value = error.limit
    changeWindowDays.value = error.windowDays
    remainingChanges.value = 0
    if (!error.retryAt) {
      return deps.translate('playerLocale.change.limitReachedUnknown')
    }
    return deps.translate('playerLocale.change.limitReached', {
      time: deps.formatRetryAt(error.retryAt),
    })
  }

  /**
   * The change is irreversible in meaning, never in data: past memories keep
   * their wording, today's schedule is replanned once, everything from here
   * on uses the new locale. The player reads that before it happens — and a
   * no-op is short-circuited *before* the dialog, so a double submit costs
   * neither a scare nor a change.
   */
  async function saveLocale(): Promise<void> {
    const current = {
      timezoneId: currentUser.value?.timezone_id ?? '',
      primaryLanguage: currentUser.value?.primary_language ?? '',
    }
    const draft = {
      timezoneId: timezoneDraft.value,
      primaryLanguage: languageDraft.value,
    }
    const payload = localeChangePayload(current, draft)
    const subject = localeChangeSubject(current, draft)
    if (!payload || !subject) {
      localeFeedback.value = deps.translate('playerLocale.change.unchanged')
      return
    }
    const label = subjectLabel(subject)
    const accepted = await deps.confirm({
      title: deps.translate('playerLocale.change.confirmTitle', { subject: label }),
      content: deps.translate('playerLocale.change.confirmBody', { subject: label }),
      okText: deps.translate('playerLocale.change.confirmOk'),
    })
    if (!accepted) return
    localeSaving.value = true
    localeFeedback.value = null
    try {
      const state = await changeLocale(payload)
      remainingChanges.value = state.remaining_changes
      changeLimit.value = state.change_limit
      changeWindowDays.value = state.window_days
      await refreshMe()
      timezoneDraft.value = currentUser.value?.timezone_id ?? timezoneDraft.value
      languageDraft.value = currentUser.value?.primary_language ?? languageDraft.value
      localeFeedback.value = deps.translate('playerLocale.change.saved')
    } catch (err) {
      localeFeedback.value = err instanceof LocaleChangeLimitError
        ? limitMessage(err)
        : failureMessage('playerLocale.change.failed', err)
    } finally {
      localeSaving.value = false
    }
  }

  /** Accepting a hint writes the *place* only — never the timezone, which
   * would silently spend a guarded change the player did not ask for. */
  async function acceptHint(): Promise<void> {
    const hint = locationHint.value
    if (!hint) return
    hintBusy.value = true
    try {
      countryCodeDraft.value = hint.country_code
      locationLabelDraft.value = hint.label ?? hint.country_code
      latitudeDraft.value = hint.latitude ?? ''
      longitudeDraft.value = hint.longitude ?? ''
      await saveLocation()
    } finally {
      hintBusy.value = false
    }
  }

  /**
   * Dismissal rides the confirm endpoint, which is free and idempotent — it
   * exists precisely so "no thanks" costs nothing from the change budget.
   */
  async function dismissHint(): Promise<void> {
    hintBusy.value = true
    localeFeedback.value = null
    try {
      await confirmLocale({ dismiss_location_hint: true })
      await refreshMe()
      locationFeedback.value = deps.translate('playerLocale.hint.dismissed')
    } catch (err) {
      locationFeedback.value = failureMessage('playerLocale.hint.failed', err)
    } finally {
      hintBusy.value = false
    }
  }

  return {
    countryCodeDraft,
    latitudeDraft,
    longitudeDraft,
    locationLabelDraft,
    locationSaving,
    locationFeedback,
    timezoneDraft,
    languageDraft,
    localeSaving,
    localeFeedback,
    hintBusy,
    remainingChanges,
    localeLimitHint,
    seedFromProfile,
    saveLocation,
    clearLocation,
    onPlaceSelected,
    onPlaceCleared,
    useCurrentLocation,
    saveLocale,
    acceptHint,
    dismissHint,
  }
}
