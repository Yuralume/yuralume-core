import type {
  CharacterCompanion,
  CharacterPersonalityType,
  CharacterState,
  CharacterVisualGenerationStyle,
  UpdateCharacterRequest,
  VisualSubjectType,
} from '@/types/character'

/**
 * The subset of `CharacterEditPanel`'s `form.value` that
 * `buildManagedAwareUpdateRequest` reads.
 */
export interface CharacterEditPersonaFormFields {
  name: string
  summary: string
  personality: string
  interests: string
  speaking_style: string
  boundaries: string
  aspirations: string
  appearance: string
  gender_identity: string
  third_person_pronoun: string
  visual_gender_presentation: string
  visual_subject_type: VisualSubjectType
  visual_generation_style: CharacterVisualGenerationStyle
  date_of_birth: string
}

function splitCommaList(s: string): string[] {
  return s.split(',').map(v => v.trim()).filter(Boolean)
}

/**
 * Builds the `PATCH` payload `CharacterEditPanel`'s `handleSave` sends.
 *
 * A plain function (not a component method) so the managed-character write
 * boundary (EC2-B) is unit testable without mounting the component. Lives
 * in its own module — not a second `<script>` block on the SFC — because
 * `<script setup>` cannot contain ES module exports at all
 * (`@vue/compiler-sfc` hard error), and the original non-`setup` `<script>`
 * sidecar block that could hold them made its own re-imported types
 * collide with `<script setup>`'s under `vue-tsc -b`'s project-reference
 * build mode (TS2300 duplicate identifier — EC2-C). Splitting into a real
 * module sidesteps both constraints at once.
 *
 * The persona fields (`name` / `summary` / `personality` / … /
 * `personality_type`) are the partner-owned half of
 * `MANAGED_WRITABLE_UPDATE_FIELDS` (backend:
 * `managed_character_update_policy.py`): sending them with a real value
 * against a managed character gets the whole request refused with a 400.
 * Rather than rely on the server's blank-echo tolerance, the client simply
 * never puts them in the payload for a managed character — only `state`,
 * `companions`, and (when the caller passes tool settings) `allowed_tools`
 * go out. An ordinary character (`isManaged === false`) gets the persona
 * fields back, unchanged from before this ticket.
 */
export function buildManagedAwareUpdateRequest(params: {
  form: CharacterEditPersonaFormFields
  state: CharacterState
  companions: CharacterCompanion[]
  personalityType: CharacterPersonalityType
  isManaged: boolean
  allowedTools: string[] | null
}): UpdateCharacterRequest {
  const { form, state, companions, personalityType, isManaged, allowedTools } = params
  const req: UpdateCharacterRequest = {
    state,
    companions: companions
      .filter(c => c.name.trim())
      .map(c => ({
        id: c.id,
        name: c.name.trim(),
        role: c.role.trim(),
        brief_profile: c.brief_profile.trim(),
        personality_sketch: c.personality_sketch
          .map(p => p.trim())
          .filter(Boolean),
        relationship_snippet: c.relationship_snippet.trim(),
      })),
  }
  if (!isManaged) {
    req.name = form.name
    req.summary = form.summary
    req.personality = splitCommaList(form.personality)
    req.interests = splitCommaList(form.interests)
    req.speaking_style = form.speaking_style
    req.boundaries = splitCommaList(form.boundaries)
    req.aspirations = splitCommaList(form.aspirations)
    req.appearance = form.appearance
    req.gender_identity = form.gender_identity.trim()
    req.third_person_pronoun = form.third_person_pronoun.trim()
    req.visual_gender_presentation = form.visual_gender_presentation.trim()
    req.visual_subject_type = form.visual_subject_type
    req.visual_generation_style = form.visual_generation_style
    req.date_of_birth = form.date_of_birth.trim() ? form.date_of_birth : null
    req.personality_type = personalityType
  }
  if (allowedTools !== null) {
    req.allowed_tools = allowedTools
  }
  return req
}
