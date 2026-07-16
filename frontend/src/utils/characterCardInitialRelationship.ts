import type {
  CharacterCardPreview,
  CharacterCreationDraftPayload,
} from '@/utils/api/characters'

export function buildCharacterCardIntakeDraft(
  card: CharacterCardPreview | null,
): CharacterCreationDraftPayload {
  if (!card) {
    return {}
  }
  return {
    name: card.name,
    summary: card.summary,
    personality: card.personality,
    interests: card.interests,
    speaking_style: card.speaking_style,
    boundaries: card.boundaries,
    aspirations: card.aspirations,
    personality_type_code: card.personality_type?.code,
    personality_type_rationale: card.personality_type?.rationale,
  }
}
