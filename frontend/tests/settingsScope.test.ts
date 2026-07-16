import { describe, expect, it } from 'vitest'

import {
  isCharacterScopeAvailable,
  resolveSettingsScope,
} from '@/utils/settingsScope'

describe('settings scope resolution', () => {
  it('disables character settings when no character is selected', () => {
    expect(isCharacterScopeAvailable(false)).toBe(false)
    expect(isCharacterScopeAvailable(true)).toBe(true)
  })

  it('falls back to personal settings when character scope loses its character', () => {
    expect(resolveSettingsScope({
      current: 'character',
      hasCharacter: false,
    })).toBe('personal')
  })

  it('keeps the selected scope while it is valid', () => {
    expect(resolveSettingsScope({
      current: 'personal',
      hasCharacter: false,
    })).toBe('personal')
    expect(resolveSettingsScope({
      current: 'personal',
      hasCharacter: true,
    })).toBe('personal')
    expect(resolveSettingsScope({
      current: 'character',
      hasCharacter: true,
    })).toBe('character')
  })
})
