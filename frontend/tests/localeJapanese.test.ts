import { afterEach, describe, expect, it } from 'vitest'

import { i18n } from '@/i18n'
import { useLocale } from '@/composables/useLocale'
import { formatDurationMinutes, formatRelativeTime } from '@/i18n/formatters'
import { antDesignLocales } from '@/i18n/antDesign'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import {
  LOCALE_LABELS,
  SOURCE_LOCALE,
  SUPPORTED_LOCALES,
  coerceLocale,
} from '@/i18n/localeTypes'

describe('Japanese locale support', () => {
  afterEach(() => {
    useLocale().resetToFallback()
  })

  it('registers ja-JP as a selectable first-class locale', () => {
    const supported = useLocale().supported.value

    expect(SUPPORTED_LOCALES).toContain('ja-JP')
    expect(LOCALE_LABELS['ja-JP']).toBe('日本語')
    expect(coerceLocale('ja-JP')).toBe('ja-JP')
    expect(supported).toContainEqual({ code: 'ja-JP', label: '日本語' })
    expect(antDesignLocales['ja-JP']).toBeTruthy()
  })

  it('applies a Japanese primary language to the runtime UI locale', () => {
    const { applyPrimaryLanguage, locale } = useLocale()

    applyPrimaryLanguage('ja-JP')

    expect(locale.value).toBe('ja-JP')
    expect(i18n.global.locale.value).toBe('ja-JP')
  })

  it('formats Japanese relative time and durations without Chinese wording', () => {
    const now = new Date('2026-05-22T12:00:00Z')
    const oneMinuteAgo = '2026-05-22T11:59:00Z'

    expect(formatRelativeTime(oneMinuteAgo, 'ja-JP', now)).toContain('分')
    expect(formatRelativeTime(oneMinuteAgo, 'ja-JP', now)).not.toContain('分鐘')
    expect(formatDurationMinutes(65, 'ja-JP')).toBe('1時間 5分')
    expect(formatDurationMinutes(5, 'ja-JP')).toBe('5分')
  })

  it('falls back unsupported primary-language values to the source locale', () => {
    const { applyPrimaryLanguage, locale } = useLocale()

    applyPrimaryLanguage('fr-FR')

    expect(locale.value).toBe(SOURCE_LOCALE)
  })

  it('renders the character edit identity warning as an actual note', () => {
    const warning = [
      jaJP.characterEdit.identityWarningLabel,
      jaJP.characterEdit.identityWarning,
    ].join('')

    expect(warning).toContain('注意：')
    expect(warning).toContain('既存の会話履歴')
    expect(warning).not.toBe('身元身元')
  })
})
