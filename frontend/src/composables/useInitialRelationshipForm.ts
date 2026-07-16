import type {
  InitialRelationshipPayload,
  ScheduleInvolvementPolicy,
} from '@/types/character'

export interface InitialRelationshipForm {
  relationship_label: string
  known_context: string
  living_arrangement: string
  user_address_name: string
  character_address_name: string
  tone_distance: string
  familiarity_boundary: string
  schedule_involvement_policy: ScheduleInvolvementPolicy
  proactive_permission: boolean
  proactive_cadence_hint: string
  user_profile_notes: string
  profile_interests: string
  profile_routine: string
  profile_life_goals: string
}

export function emptyInitialRelationshipForm(): InitialRelationshipForm {
  return {
    relationship_label: '',
    known_context: '',
    living_arrangement: '',
    user_address_name: '',
    character_address_name: '',
    tone_distance: '',
    familiarity_boundary: '',
    schedule_involvement_policy: 'none',
    proactive_permission: false,
    proactive_cadence_hint: '',
    user_profile_notes: '',
    profile_interests: '',
    profile_routine: '',
    profile_life_goals: '',
  }
}

export function splitList(value: string): string[] {
  return value
    .split(/[,，\n]/)
    .map(item => item.trim())
    .filter(Boolean)
}

export function buildInitialRelationshipPayload(
  rel: InitialRelationshipForm,
): InitialRelationshipPayload | null {
  const safeProfile = {
    interests: splitList(rel.profile_interests),
    routine: rel.profile_routine.trim(),
    life_goals: splitList(rel.profile_life_goals),
  }
  const hasSafeProfile = Boolean(
    safeProfile.interests.length
    || safeProfile.routine
    || safeProfile.life_goals.length,
  )
  const payload: InitialRelationshipPayload = {
    relationship_label: rel.relationship_label.trim(),
    known_context: rel.known_context.trim(),
    living_arrangement: rel.living_arrangement.trim(),
    user_address_name: rel.user_address_name.trim(),
    character_address_name: rel.character_address_name.trim(),
    tone_distance: rel.tone_distance.trim(),
    familiarity_boundary: rel.familiarity_boundary.trim(),
    schedule_involvement_policy: rel.schedule_involvement_policy,
    proactive_permission: rel.proactive_permission,
    proactive_cadence_hint: rel.proactive_cadence_hint.trim(),
    user_profile_notes: rel.user_profile_notes.trim(),
    confirmed_by_user: true,
    safe_user_profile: hasSafeProfile ? safeProfile : undefined,
  }
  const hasValues = Boolean(
    payload.relationship_label
    || payload.known_context
    || payload.living_arrangement
    || payload.user_address_name
    || payload.character_address_name
    || payload.tone_distance
    || payload.familiarity_boundary
    || payload.schedule_involvement_policy !== 'none'
    || payload.proactive_permission
    || payload.proactive_cadence_hint
    || payload.user_profile_notes
    || hasSafeProfile,
  )
  return hasValues ? payload : null
}
