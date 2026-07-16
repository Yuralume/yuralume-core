import { computed, ref } from 'vue'

const DEFAULT_TIMEZONE = 'UTC'
const FALLBACK_TIMEZONES = [
  'UTC',
  'Asia/Taipei',
  'Asia/Tokyo',
  'Asia/Seoul',
  'Asia/Hong_Kong',
  'Asia/Singapore',
  'Asia/Shanghai',
  'Europe/London',
  'Europe/Berlin',
  'Europe/Paris',
  'America/New_York',
  'America/Chicago',
  'America/Denver',
  'America/Los_Angeles',
]

export interface TimezoneOption {
  value: string
  label: string
}

function isValidTimezone(value: string): boolean {
  const timezone = value.trim()
  if (!timezone) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: timezone }).format(new Date())
    return true
  } catch {
    return false
  }
}

function browserTimezone(): string {
  if (typeof Intl === 'undefined') return DEFAULT_TIMEZONE
  const detected = Intl.DateTimeFormat().resolvedOptions().timeZone
  return detected && isValidTimezone(detected) ? detected : DEFAULT_TIMEZONE
}

const activeTimezone = ref<string>(browserTimezone())

function supportedTimezones(): string[] {
  if (typeof Intl !== 'undefined') {
    const intlWithValues = Intl as typeof Intl & {
      supportedValuesOf?: (key: 'timeZone') => string[]
    }
    const values = intlWithValues.supportedValuesOf?.('timeZone') ?? []
    if (values.length > 0) {
      const withUtc = values.includes(DEFAULT_TIMEZONE)
        ? values
        : [DEFAULT_TIMEZONE, ...values]
      return [...new Set(withUtc)].sort((a, b) => a.localeCompare(b))
    }
  }
  return FALLBACK_TIMEZONES
}

export function useTimezone() {
  const timeZone = computed(() => activeTimezone.value)
  const timezoneOptions = computed<TimezoneOption[]>(() =>
    supportedTimezones().map((value) => ({ value, label: value })),
  )

  function applyUserTimezone(timezoneId: string | null | undefined): void {
    const next = (timezoneId ?? '').trim()
    activeTimezone.value = isValidTimezone(next) ? next : DEFAULT_TIMEZONE
  }

  function resetToBrowserTimezone(): void {
    activeTimezone.value = browserTimezone()
  }

  return {
    timeZone,
    timezoneOptions,
    browserTimezone,
    isValidTimezone,
    applyUserTimezone,
    resetToBrowserTimezone,
  }
}
