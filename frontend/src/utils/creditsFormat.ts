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

/**
 * The i18n calls needed to render an amount, narrowed to what this helper
 * uses so `t` can be handed in from any component without importing vue-i18n
 * here (the unit *word* and its en-US plural stay in the catalog).
 */
export interface CreditTranslate {
  (key: string, plural: number): string
  (key: string, named: Record<string, unknown>): string
}

/**
 * "5,340 螢火" / "5,340 Lumes" — the one place the number and the unit word
 * are joined, shared by the balance badge and every price hint so a future
 * wording change lands on all of them at once.
 */
export function creditAmountText(
  translate: CreditTranslate,
  value: number,
): string {
  return translate('credits.amount', {
    amount: formatCreditAmount(value),
    // Plural count for en-US (Lume / Lumes); zh-TW and ja-JP resolve to the
    // same word either way.
    unit: translate('credits.unit', Math.round(Math.abs(value))),
  })
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

/**
 * Absolute URL of the Portal itself (the account centre home), or `null` on
 * the same terms as `portalCreditsUrl`. Shared so the components that render
 * the entry and the ones that decide *whether* to render it agree on one
 * normalization instead of each trimming the trailing slash themselves.
 */
export function portalHomeUrl(portalUrl: string | null): string | null {
  return portalPath(portalUrl, '')
}

export function portalPath(
  portalUrl: string | null,
  path: string,
): string | null {
  const base = (portalUrl ?? '').trim().replace(/\/+$/, '')
  if (!base) return null
  return `${base}${path}`
}
