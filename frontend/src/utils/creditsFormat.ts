/**
 * Presentation helpers for the hosted credit unit.
 *
 * The unit *word* is owned by i18n (`credits.unit`, pluralized for en-US) and
 * must stay aligned with the Portal's `creditsPresentation.ts`
 * (zh-TW 螢火 / ja-JP 蛍火 / en-US Lume·Lumes). Only the number formatting
 * lives here.
 *
 * Unlike the Portal ledger — which always shows two decimals because a single
 * row can be a fraction of a credit — the in-game badge shows a balance, so a
 * whole number renders without a trailing `.00`.
 */

export function formatCreditAmount(amount: number): string {
  if (!Number.isFinite(amount)) return '0'
  const rounded = round2(amount)
  const whole = Number.isInteger(rounded)
  const [integerPart, fraction] = Math.abs(rounded)
    .toFixed(whole ? 0 : 2)
    .split('.')
  const grouped = integerPart.replace(/\B(?=(\d{3})+(?!\d))/g, ',')
  const sign = rounded < 0 ? '-' : ''
  return fraction ? `${sign}${grouped}.${fraction}` : `${sign}${grouped}`
}

function round2(value: number): number {
  const rounded = Math.round(value * 100) / 100
  // Guard -0 so a zero balance never renders with a stray minus sign.
  return rounded === 0 ? 0 : rounded
}

/**
 * Absolute URL of the Portal ledger / top-up page, or `null` when the
 * deployment did not advertise a Portal (self-host, or cloud without
 * `YURALUME_CLOUD_PORTAL_URL`). Callers render the link only when non-null.
 */
export function portalCreditsUrl(portalUrl: string | null): string | null {
  return portalPath(portalUrl, '/credits')
}

export function portalPath(
  portalUrl: string | null,
  path: string,
): string | null {
  const base = (portalUrl ?? '').trim().replace(/\/+$/, '')
  if (!base) return null
  return `${base}${path}`
}
