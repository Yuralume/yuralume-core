/**
 * Pure decision logic for the hosted locale lifecycle (plan G2).
 *
 * Kept out of the components so the rules that actually matter can be
 * unit-tested without a DOM:
 *
 *   1. *when* the blocking first-login confirmation may cover the app,
 *   2. *what* a locale save is about to change — which decides both whether
 *      to spend one of the player's two monthly changes at all and which
 *      irreversibility warning to show, and
 *   3. *what to say* when the confirmation endpoint refuses, which is the
 *      difference between sending a player to the right field and leaving
 *      them re-pressing a button that will never work.
 */

import {
  LocaleAlreadyConfirmedError,
  LocaleChangeInProgressError,
} from '@/utils/api/playerLocale'

/** Which identity field(s) a pending save would move. */
export type LocaleChangeSubject = 'timezone' | 'language' | 'both'

export interface LocaleSnapshot {
  timezoneId: string
  primaryLanguage: string
}

export interface LocaleGateInput {
  /** Hosted deployment? Self-host never blocks — it has no such flow. */
  cloudMode: boolean
  /** A resolved identity exists (login / token bootstrap finished). */
  authenticated: boolean
  /** `GET /auth/me` says this player has not acknowledged their locale. */
  needsConfirmation: boolean
  /**
   * The current route is `meta.public` (login, setup, OAuth callbacks).
   * Blocking there would deadlock the very flow that produces the identity
   * the gate needs.
   */
  publicRoute: boolean
}

/**
 * The onboarding gate is deliberately conservative: every condition must
 * hold, so any uncertainty (not yet bootstrapped, mid-login, self-host)
 * resolves to "let the player through".
 */
export function shouldBlockForLocaleConfirmation(
  input: LocaleGateInput,
): boolean {
  if (!input.cloudMode) return false
  if (input.publicRoute) return false
  if (!input.authenticated) return false
  return input.needsConfirmation
}

function normalize(value: string | null | undefined): string {
  return (value ?? '').trim()
}

/**
 * Build the `PATCH /auth/me/locale` body for a draft, or `null` when
 * nothing actually moved.
 *
 * The null case is load-bearing: a double-submitted form must not spend a
 * change from the 30-day window. The backend no-ops equal values too, but
 * not asking at all also spares the player a pointless warning dialog.
 */
export function localeChangePayload(
  current: LocaleSnapshot,
  draft: LocaleSnapshot,
): { timezone_id?: string; primary_language?: string } | null {
  const payload: { timezone_id?: string; primary_language?: string } = {}
  const timezoneId = normalize(draft.timezoneId)
  const primaryLanguage = normalize(draft.primaryLanguage)
  if (timezoneId && timezoneId !== normalize(current.timezoneId)) {
    payload.timezone_id = timezoneId
  }
  if (primaryLanguage && primaryLanguage !== normalize(current.primaryLanguage)) {
    payload.primary_language = primaryLanguage
  }
  return payload.timezone_id || payload.primary_language ? payload : null
}

/** A message key plus whatever the wording needs interpolated into it. */
export interface LocaleFailureCopy {
  key: string
  params?: Record<string, unknown>
  /**
   * The refusal says the server's view of this account has moved past what
   * the screen believes, so the identity is worth re-reading — for the
   * onboarding gate that is what actually dismisses it.
   */
  staleGate: boolean
}

/**
 * Turn a refusal from `POST /auth/me/locale/confirm` into the copy the
 * onboarding gate should show, or `null` for anything that is a plain
 * failure and belongs on the generic "that didn't save" path.
 *
 * The two refusals are opposites and must not share wording: "already
 * confirmed" means *stop pressing this button, the setting lives elsewhere
 * now*, while "in progress" means *press it again in a moment*.
 */
export function localeConfirmFailureCopy(
  error: unknown,
): LocaleFailureCopy | null {
  if (error instanceof LocaleAlreadyConfirmedError) {
    return {
      key: 'playerLocale.confirm.alreadyConfirmed',
      params: { limit: error.limit, days: error.windowDays },
      staleGate: true,
    }
  }
  if (error instanceof LocaleChangeInProgressError) {
    return { key: 'playerLocale.change.inProgress', staleGate: false }
  }
  return null
}

/** Which warning wording a pending change needs; `null` when it is a no-op. */
export function localeChangeSubject(
  current: LocaleSnapshot,
  draft: LocaleSnapshot,
): LocaleChangeSubject | null {
  const payload = localeChangePayload(current, draft)
  if (!payload) return null
  if (payload.timezone_id && payload.primary_language) return 'both'
  return payload.timezone_id ? 'timezone' : 'language'
}
