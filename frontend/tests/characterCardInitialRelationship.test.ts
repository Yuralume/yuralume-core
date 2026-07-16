import { describe, expect, it } from 'vitest'
import { buildCharacterCardIntakeDraft } from '@/utils/characterCardInitialRelationship'
import type { CharacterCardPreview } from '@/utils/api/characters'

describe('buildCharacterCardIntakeDraft', () => {
  it('maps portable card preview fields into the creation-intake draft', () => {
    const card: CharacterCardPreview = {
      pack_id: 'mio',
      title: 'Mio Card',
      author: 'tester',
      description: '',
      tags: [],
      note: '',
      name: 'Mio',
      summary: 'A quiet cafe owner.',
      personality: ['calm', 'observant'],
      interests: ['coffee', 'journals'],
      speaking_style: 'soft',
      boundaries: ['no invented shared memories'],
      aspirations: ['open a second cafe'],
      appearance: '',
      gender_identity: '',
      third_person_pronoun: '',
      visual_gender_presentation: '',
      visual_subject_type: 'human',
      date_of_birth: null,
      disposition: {
        self_centeredness: 'medium',
        candor: 'medium',
        sharing_drive: 'medium',
        associativeness: 'medium',
      },
      personality_type: {
        system: 'mbti_16',
        code: 'INFJ',
        source: 'llm_inferred',
        confidence: 0.7,
        rationale: 'Warm but reflective.',
        consistency_notes: [],
      },
      world_frame: 'modern',
      world_awareness_enabled: false,
      world_topics: [],
      subscribed_categories: [],
      excluded_topics: [],
      proactive_enabled: false,
      proactive_daily_limit: 0,
      proactive_cooldown_minutes: 0,
      accepts_web_proactive: false,
      feed_daily_limit: 0,
      companions: [],
      has_main_arc: false,
      bundled_arc_template_count: 0,
      bundled_arc_titles: [],
      has_arc_series: false,
      bundled_arc_series_count: 0,
      bundled_arc_series_titles: [],
      bundled_arc_series_member_count: 0,
      stage_image_count: 0,
      image_urls: [],
    }

    expect(buildCharacterCardIntakeDraft(card)).toEqual({
      name: 'Mio',
      summary: 'A quiet cafe owner.',
      personality: ['calm', 'observant'],
      interests: ['coffee', 'journals'],
      speaking_style: 'soft',
      boundaries: ['no invented shared memories'],
      aspirations: ['open a second cafe'],
      personality_type_code: 'INFJ',
      personality_type_rationale: 'Warm but reflective.',
    })
  })

  it('returns an empty draft when the wizard has no card context', () => {
    expect(buildCharacterCardIntakeDraft(null)).toEqual({})
  })
})
