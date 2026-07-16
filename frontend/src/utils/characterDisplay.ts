import type { Character } from '@/types/character'

export type CharacterDisplayFacts = Pick<
  Character,
  'name' | 'third_person_pronoun'
>

export function characterDisplayRef(
  character: CharacterDisplayFacts | null | undefined,
  fallback: string,
): string {
  const name = character?.name?.trim()
  return name || fallback
}

export function characterPronoun(
  character: CharacterDisplayFacts | null | undefined,
): string {
  return character?.third_person_pronoun?.trim() ?? ''
}

export function characterPossessiveLabel(
  character: CharacterDisplayFacts | null | undefined,
  suffix: string,
  fallbackPrefix: string,
): string {
  const ref = characterPronoun(character) || characterDisplayRef(character, fallbackPrefix)
  return `${ref}${suffix}`
}
