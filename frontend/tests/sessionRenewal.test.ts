/**
 * When an in-progress session gets extended.
 *
 * Hosted sessions run on a fixed TTL (12h shipped), so before this a player
 * mid-conversation was signed out on a wall-clock timer. Renewal is gated on
 * *recent interaction* rather than "the tab is open": an abandoned tab must
 * not hold a hosted session — and its paid tier — alive indefinitely.
 */

import { describe, expect, it, vi } from 'vitest'

import {
  ACTIVITY_WINDOW_MS,
  RENEW_WHEN_REMAINING_RATIO,
  createSessionRenewer,
  readSessionClaims,
  shouldRenewSession,
} from '@/utils/sessionRenewal'

function jwtWith(payload: Record<string, unknown>): string {
  const encode = (value: object) =>
    btoa(JSON.stringify(value)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
  return `${encode({ alg: 'HS256' })}.${encode(payload)}.signature`
}

const NOW = 1_800_000_000_000 // ms
const HOUR = 60 * 60 * 1000

describe('readSessionClaims', () => {
  it('reads iat/exp as milliseconds', () => {
    const token = jwtWith({ sub: 'u1', iat: 1_800_000_000, exp: 1_800_003_600 })

    expect(readSessionClaims(token)).toEqual({
      issuedAt: 1_800_000_000_000,
      expiresAt: 1_800_003_600_000,
    })
  })

  it('degrades to unknown rather than throwing on anything unparseable', () => {
    // The claims are advisory — a token shape we cannot read must simply
    // disable renewal, never break the app that is otherwise working.
    const unknown = { issuedAt: null, expiresAt: null }
    expect(readSessionClaims(null)).toEqual(unknown)
    expect(readSessionClaims('')).toEqual(unknown)
    expect(readSessionClaims('not-a-jwt')).toEqual(unknown)
    expect(readSessionClaims('a.b.c')).toEqual(unknown)
    expect(readSessionClaims(jwtWith({ sub: 'u1' }))).toEqual(unknown)
    expect(readSessionClaims(jwtWith({ exp: 'soon' }))).toEqual(unknown)
  })
})

describe('shouldRenewSession', () => {
  const activeSession = (remainingMs: number, totalMs = 12 * HOUR) => ({
    claims: {
      issuedAt: NOW + remainingMs - totalMs,
      expiresAt: NOW + remainingMs,
    },
    lastActivityAt: NOW - 60_000,
    now: NOW,
  })

  it('renews once the session is inside its final quarter', () => {
    const threshold = 12 * HOUR * RENEW_WHEN_REMAINING_RATIO

    expect(shouldRenewSession(activeSession(threshold - 1000))).toBe(true)
    expect(shouldRenewSession(activeSession(threshold + 1000))).toBe(false)
  })

  it('does not renew for a player who walked away', () => {
    expect(shouldRenewSession({
      ...activeSession(HOUR),
      lastActivityAt: NOW - ACTIVITY_WINDOW_MS - 1000,
    })).toBe(false)
  })

  it('renews for a player who is still interacting', () => {
    expect(shouldRenewSession({
      ...activeSession(HOUR),
      lastActivityAt: NOW - ACTIVITY_WINDOW_MS + 1000,
    })).toBe(true)
  })

  it('leaves an already-expired session to the 401 bounce', () => {
    // Renewal would just 401 too; the bounce owns that path and knows where
    // to send the player.
    expect(shouldRenewSession(activeSession(-1000))).toBe(false)
  })

  it('does nothing when the token carries no readable expiry', () => {
    expect(shouldRenewSession({
      claims: { issuedAt: null, expiresAt: null },
      lastActivityAt: NOW,
      now: NOW,
    })).toBe(false)
  })

  it('falls back to a fixed window when only the expiry is readable', () => {
    // Without iat there is no total lifetime to take a fraction of.
    const remaining = (expiresInMs: number) => ({
      claims: { issuedAt: null, expiresAt: NOW + expiresInMs },
      lastActivityAt: NOW,
      now: NOW,
    })

    expect(shouldRenewSession(remaining(60 * 1000))).toBe(true)
    expect(shouldRenewSession(remaining(6 * HOUR))).toBe(false)
  })

  it('handles a clock skew that makes the session look longer than it is', () => {
    // issuedAt in the future ⇒ negative total lifetime; must not turn into a
    // negative threshold that suppresses renewal until the token is dead.
    expect(shouldRenewSession({
      claims: { issuedAt: NOW + HOUR, expiresAt: NOW + 60 * 1000 },
      lastActivityAt: NOW,
      now: NOW,
    })).toBe(true)
  })
})

describe('createSessionRenewer', () => {
  function renewerFor(options: {
    remainingMs: number
    renew?: () => Promise<void>
    isEnabled?: () => boolean
  }) {
    let clock = NOW
    const renew = vi.fn(options.renew ?? (async () => undefined))
    const renewer = createSessionRenewer({
      isEnabled: options.isEnabled ?? (() => true),
      readToken: () => jwtWith({
        sub: 'u1',
        iat: (NOW + options.remainingMs - 12 * HOUR) / 1000,
        exp: (NOW + options.remainingMs) / 1000,
      }),
      renew,
      now: () => clock,
    })
    return { renewer, renew, advance: (ms: number) => { clock += ms } }
  }

  it('renews a session that is running out while the player is here', async () => {
    const { renewer, renew } = renewerFor({ remainingMs: HOUR })

    await renewer.maybeRenew()

    expect(renew).toHaveBeenCalledOnce()
  })

  it('leaves an abandoned tab alone', async () => {
    // Otherwise an open tab would hold a hosted session — and the paid tier
    // behind it — alive forever.
    const { renewer, renew, advance } = renewerFor({ remainingMs: HOUR })
    advance(ACTIVITY_WINDOW_MS + 1000)

    await renewer.maybeRenew()

    expect(renew).not.toHaveBeenCalled()
  })

  it('wakes back up when the player returns', async () => {
    const { renewer, renew, advance } = renewerFor({ remainingMs: HOUR })
    advance(ACTIVITY_WINDOW_MS + 1000)
    renewer.markActive()

    await renewer.maybeRenew()

    expect(renew).toHaveBeenCalledOnce()
  })

  it('does nothing when auth is disabled', async () => {
    const { renewer, renew } = renewerFor({
      remainingMs: HOUR,
      isEnabled: () => false,
    })

    await renewer.maybeRenew()

    expect(renew).not.toHaveBeenCalled()
  })

  it('single-flights concurrent attempts', async () => {
    // Held in an object so narrowing doesn't decide the executor's assignment
    // never happens.
    const gate: { release: () => void } = { release: () => {} }
    const { renewer, renew } = renewerFor({
      remainingMs: HOUR,
      renew: () => new Promise<void>((resolve) => { gate.release = resolve }),
    })

    const first = renewer.maybeRenew()
    await renewer.maybeRenew()
    gate.release()
    await first

    expect(renew).toHaveBeenCalledOnce()
  })

  it('swallows a failed renewal and stays eligible to retry', async () => {
    // The token we hold is still valid — a transient failure must not turn
    // into a sign-out.
    const { renewer, renew } = renewerFor({
      remainingMs: HOUR,
      renew: async () => { throw new Error('network') },
    })

    await expect(renewer.maybeRenew()).resolves.toBeUndefined()
    await renewer.maybeRenew()

    expect(renew).toHaveBeenCalledTimes(2)
  })
})
