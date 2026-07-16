/**
 * Centralised date / time / duration formatters keyed to the active
 * UI locale.
 *
 * Why centralised: docs/FRONTEND_I18N_PLAN.md §Formatter 規則 calls
 * out the risk of every component hand-writing `分鐘前` / `toLocaleString`.
 * One module = one place to tune.
 *
 * Time-zone strategy: callers pass the active user IANA timezone.
 * Browser-local defaults are intentionally avoided for user-visible
 * dates so DB / server can stay UTC while the UI renders user civil time.
 */

import type { SupportedLocale } from './localeTypes'

type LocaleInput = SupportedLocale | string

function parseIso(iso: string): Date | null {
  if (!iso) return null
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? null : d
}

interface DateParts {
  year: string
  month: string
  day: string
}

function withTimeZone(
  options: Intl.DateTimeFormatOptions,
  timeZone?: string,
): Intl.DateTimeFormatOptions {
  return timeZone ? { ...options, timeZone } : options
}

function isoDateParts(date: Date, timeZone: string): DateParts | null {
  try {
    const parts = new Intl.DateTimeFormat('en-CA', {
      timeZone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(date)
    const year = parts.find((p) => p.type === 'year')?.value
    const month = parts.find((p) => p.type === 'month')?.value
    const day = parts.find((p) => p.type === 'day')?.value
    return year && month && day ? { year, month, day } : null
  } catch {
    return null
  }
}

export function todayISOForTimezone(
  timeZone: string,
  now: Date = new Date(),
): string {
  const parts = isoDateParts(now, timeZone)
  if (!parts) return now.toISOString().slice(0, 10)
  return `${parts.year}-${parts.month}-${parts.day}`
}

export function addCivilDays(isoDate: string, deltaDays: number): string {
  const [year, month, day] = isoDate.split('-').map(Number)
  if (!year || !month || !day) return isoDate
  const next = new Date(Date.UTC(year, month - 1, day + deltaDays))
  return next.toISOString().slice(0, 10)
}

export function timeInputValueForTimezone(iso: string, timeZone: string): string {
  const d = parseIso(iso)
  if (!d) return ''
  try {
    const parts = new Intl.DateTimeFormat('en-GB', {
      timeZone,
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(d)
    const hour = parts.find((p) => p.type === 'hour')?.value
    const minute = parts.find((p) => p.type === 'minute')?.value
    return hour && minute ? `${hour}:${minute}` : ''
  } catch {
    return ''
  }
}

export function formatDateTime(
  iso: string,
  locale: LocaleInput,
  timeZone?: string,
): string {
  const d = parseIso(iso)
  if (!d) return iso
  return new Intl.DateTimeFormat(locale, withTimeZone({
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }, timeZone)).format(d)
}

export function formatDate(
  iso: string,
  locale: LocaleInput,
  timeZone?: string,
): string {
  const d = parseIso(iso)
  if (!d) return iso
  return new Intl.DateTimeFormat(locale, withTimeZone({
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }, timeZone)).format(d)
}

export function formatTime(
  iso: string,
  locale: LocaleInput,
  timeZone?: string,
): string {
  const d = parseIso(iso)
  if (!d) return iso
  return new Intl.DateTimeFormat(locale, withTimeZone({
    hour: '2-digit',
    minute: '2-digit',
  }, timeZone)).format(d)
}

export function formatTimeRange(
  startIso: string,
  endIso: string,
  locale: LocaleInput,
  timeZone?: string,
): string {
  const start = parseIso(startIso)
  const end = parseIso(endIso)
  if (!start || !end) return `${startIso}–${endIso}`
  const startDay = timeZone
    ? todayISOForTimezone(timeZone, start)
    : startIso.slice(0, 10)
  const endDay = timeZone
    ? todayISOForTimezone(timeZone, end)
    : endIso.slice(0, 10)
  const sameDay = startDay === endDay
  const startStr = sameDay
    ? formatTime(startIso, locale, timeZone)
    : formatDateTime(startIso, locale, timeZone)
  const endStr = formatTime(endIso, locale, timeZone)
  return `${startStr}–${endStr}`
}

/**
 * Render a relative time string ("3 minutes ago", "3 分鐘前") for an
 * ISO timestamp. Uses `Intl.RelativeTimeFormat` so the language
 * preposition + plural agreement is correct for the active locale.
 */
export function formatRelativeTime(
  iso: string,
  locale: LocaleInput,
  now: Date = new Date(),
): string {
  const d = parseIso(iso)
  if (!d) return iso
  const diffSec = Math.round((d.getTime() - now.getTime()) / 1000)
  const abs = Math.abs(diffSec)
  const rtf = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  if (abs < 60) return rtf.format(diffSec, 'second')
  const diffMin = Math.round(diffSec / 60)
  if (Math.abs(diffMin) < 60) return rtf.format(diffMin, 'minute')
  const diffHour = Math.round(diffSec / 3600)
  if (Math.abs(diffHour) < 24) return rtf.format(diffHour, 'hour')
  const diffDay = Math.round(diffSec / 86400)
  if (Math.abs(diffDay) < 30) return rtf.format(diffDay, 'day')
  const diffMonth = Math.round(diffDay / 30)
  if (Math.abs(diffMonth) < 12) return rtf.format(diffMonth, 'month')
  return rtf.format(Math.round(diffDay / 365), 'year')
}

/**
 * Localised duration string for a span of minutes. Used in places like
 * the schedule card's "next activity in 25 min". Keeps the formatting
 * out of each consumer so a future change (e.g. switching to
 * "25m" / "1h 5m") happens in one place.
 */
export function formatDurationMinutes(
  minutes: number,
  locale: LocaleInput,
): string {
  if (!Number.isFinite(minutes)) return '—'
  const total = Math.max(0, Math.round(minutes))
  const hours = Math.floor(total / 60)
  const mins = total % 60
  if (locale === 'en-US') {
    if (hours > 0 && mins > 0) return `${hours}h ${mins}m`
    if (hours > 0) return `${hours}h`
    return `${mins}m`
  }
  if (locale === 'ja-JP') {
    if (hours > 0 && mins > 0) return `${hours}時間 ${mins}分`
    if (hours > 0) return `${hours}時間`
    return `${mins}分`
  }
  // zh-TW (and future zh-*) — Chinese duration words.
  if (hours > 0 && mins > 0) return `${hours} 小時 ${mins} 分`
  if (hours > 0) return `${hours} 小時`
  return `${mins} 分鐘`
}
