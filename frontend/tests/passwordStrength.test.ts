import { describe, expect, it } from 'vitest'
import { passwordStrengthLevel } from '@/utils/passwordStrength'

describe('passwordStrengthLevel', () => {
  it('tracks the backend floor rather than a client-only opinion', () => {
    expect(passwordStrengthLevel('short1', 8)).toBe('tooShort')
    expect(passwordStrengthLevel('exactly8', 8)).not.toBe('tooShort')
  })

  it('does not read a long single-class run as strong', () => {
    // 12+ characters but only one character class — length alone is not
    // enough to call this "strong".
    expect(passwordStrengthLevel('aaaaaaaaaaaa')).toBe('fair')
  })

  it('rewards length and variety together as strong', () => {
    expect(passwordStrengthLevel('Correct-Horse-42')).toBe('strong')
  })

  it('calls a short but varied password merely fair, not strong', () => {
    expect(passwordStrengthLevel('Ab3!xyz9')).toBe('fair')
  })

  it('calls a plain 8-character password weak', () => {
    expect(passwordStrengthLevel('abcdefgh')).toBe('weak')
  })
})
