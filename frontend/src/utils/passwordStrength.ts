/**
 * A minimal, dependency-free password-strength hint (CB4).
 *
 * Not a security control — the backend's only hard rule is an 8-character
 * floor (``MIN_BACKUP_PASSWORD_LENGTH``). This is purely a UI nudge toward
 * the 12+ length the plan recommends, scored on length plus character-class
 * variety so "aaaaaaaaaaaa" doesn't read as strong.
 */

export type PasswordStrengthLevel = 'tooShort' | 'weak' | 'fair' | 'strong'

const RECOMMENDED_LENGTH = 12

function characterClassCount(password: string): number {
  let classes = 0
  if (/[a-z]/.test(password)) classes += 1
  if (/[A-Z]/.test(password)) classes += 1
  if (/[0-9]/.test(password)) classes += 1
  if (/[^a-zA-Z0-9]/.test(password)) classes += 1
  return classes
}

/**
 * Score a candidate backup password. ``minLength`` mirrors the backend's
 * floor (``MIN_BACKUP_PASSWORD_LENGTH`` = 8) so "too short" tracks the
 * server's own refusal rather than a client-only opinion.
 */
export function passwordStrengthLevel(
  password: string,
  minLength = 8,
): PasswordStrengthLevel {
  if (password.length < minLength) return 'tooShort'
  const classes = characterClassCount(password)
  if (password.length >= RECOMMENDED_LENGTH && classes >= 3) return 'strong'
  if (password.length >= RECOMMENDED_LENGTH || classes >= 3) return 'fair'
  return 'weak'
}
