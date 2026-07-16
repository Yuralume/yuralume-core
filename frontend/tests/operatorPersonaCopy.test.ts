import { describe, expect, it } from 'vitest'

import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { messages as zhTW } from '@/i18n/locales/zh-TW'

describe('operator persona relationship copy', () => {
  it('renders familiarity bands as interaction heat, not relationship stages', () => {
    expect(zhTW.operatorPersona.familiarity.stranger).toBe('互動還很少')
    expect(zhTW.operatorPersona.strength.daysKnown).toContain('互動已持續')
    expect(zhTW.characterCreate.initialRelationship.hint).not.toContain('初識')

    expect(enUS.operatorPersona.familiarity.stranger).toBe('Low interaction')
    expect(enUS.operatorPersona.strength.daysKnown).not.toContain('Known for')

    expect(jaJP.operatorPersona.familiarity.stranger).toContain('やり取り')
    expect(jaJP.operatorPersona.strength.daysKnown).toContain('やり取り')
  })
})
