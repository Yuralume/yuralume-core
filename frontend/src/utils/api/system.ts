import axios from 'axios'

export async function listProviders(): Promise<string[]> {
  const { data } = await axios.get<string[]>('/api/v1/system/providers')
  return data
}

export async function listProviderModels(providerId: string): Promise<string[]> {
  const { data } = await axios.get<string[]>(
    `/api/v1/system/providers/${encodeURIComponent(providerId)}/models`,
  )
  return data
}

export interface ActiveModelPreference {
  provider_id: string | null
  model_id: string | null
  /** Tri-state vision pin on the primary pick: ``null`` inherits the
   * provider connection's flag, ``true`` / ``false`` override it (one
   * aggregator connection fronts both vision and text-only models). */
  supports_vision?: boolean | null
}

export type RoutingPreferenceScope = 'user' | 'global'

function scopeParams(scope: RoutingPreferenceScope = 'global') {
  return { params: { scope } }
}

export async function getActiveModelPreference(
  scope: RoutingPreferenceScope = 'global',
): Promise<ActiveModelPreference> {
  const { data } = await axios.get<ActiveModelPreference>(
    '/api/v1/system/preferences/active-model',
    scopeParams(scope),
  )
  return data
}

export async function setActiveModelPreference(
  preference: ActiveModelPreference,
  scope: RoutingPreferenceScope = 'global',
): Promise<ActiveModelPreference> {
  const { data } = await axios.put<ActiveModelPreference>(
    '/api/v1/system/preferences/active-model',
    preference,
    scopeParams(scope),
  )
  return data
}

/** Routing-level reasoning posture riding on a feature/group entry.
 * When any field is set, the backend resolver replaces the provider
 * connection's reasoning settings with this trio for calls routed
 * through the entry. All-default = treated as absent. */
export interface FeatureReasoningOverride {
  disable_reasoning: boolean
  reasoning_effort: string | null
  thinking_budget_tokens: number | null
}

export function emptyReasoningOverride(): FeatureReasoningOverride {
  return {
    disable_reasoning: false,
    reasoning_effort: null,
    thinking_budget_tokens: null,
  }
}

export function hasReasoningOverride(
  reasoning: FeatureReasoningOverride | null | undefined,
): boolean {
  if (!reasoning) return false
  return Boolean(
    reasoning.disable_reasoning ||
      (reasoning.reasoning_effort ?? '').trim() ||
      reasoning.thinking_budget_tokens,
  )
}

export interface FeatureModelEntry {
  provider_id: string | null
  model_id: string | null
  reasoning?: FeatureReasoningOverride | null
  /** Tri-state vision pin overriding the provider connection's
   * ``supports_vision`` flag for calls routed through this entry.
   * ``null`` inherits; ``true`` / ``false`` pin it. May be the only
   * thing an entry sets. */
  supports_vision?: boolean | null
}

export interface FeatureModelsPreference {
  /** provider/model overrides, keyed by backend feature_key. */
  overrides: Record<string, FeatureModelEntry>
  /** Catalogue of feature keys the backend knows about — frontend
   * renders a picker row for each of these. */
  known_keys: string[]
  /** Human-readable zh-TW labels, same key set as ``known_keys``. */
  labels: Record<string, string>
}

export interface FeatureGroupMember {
  key: string
  label: string
}

export interface FeatureModelGroupSummary {
  key: string
  label: string
  description: string
  model_guidance: string
  members: FeatureGroupMember[]
  model: FeatureModelEntry | null
}

export interface FeatureModelGroupsPreference {
  groups: FeatureModelGroupSummary[]
  active_model: ActiveModelPreference
}

export interface FeatureModelGroupsUpdate {
  feature_model_groups: Record<string, FeatureModelEntry>
}

export async function getFeatureModelPreferences(
  scope: RoutingPreferenceScope = 'global',
): Promise<FeatureModelsPreference> {
  const { data } = await axios.get<FeatureModelsPreference>(
    '/api/v1/system/preferences/feature-models',
    scopeParams(scope),
  )
  return data
}

export async function setFeatureModelPreferences(
  preference: FeatureModelsPreference,
  scope: RoutingPreferenceScope = 'global',
): Promise<FeatureModelsPreference> {
  const { data } = await axios.put<FeatureModelsPreference>(
    '/api/v1/system/preferences/feature-models',
    preference,
    scopeParams(scope),
  )
  return data
}

export interface TTSPregenerationPreference {
  enabled: boolean
}

export async function getTTSPregenerationPreference(): Promise<TTSPregenerationPreference> {
  const { data } = await axios.get<TTSPregenerationPreference>(
    '/api/v1/system/preferences/tts-pregeneration',
  )
  return data
}

export async function setTTSPregenerationPreference(
  preference: TTSPregenerationPreference,
): Promise<TTSPregenerationPreference> {
  const { data } = await axios.put<TTSPregenerationPreference>(
    '/api/v1/system/preferences/tts-pregeneration',
    preference,
  )
  return data
}

export async function getFeatureModelGroups(
  scope: RoutingPreferenceScope = 'global',
): Promise<FeatureModelGroupsPreference> {
  const { data } = await axios.get<FeatureModelGroupsPreference>(
    '/api/v1/system/preferences/feature-model-groups',
    scopeParams(scope),
  )
  return data
}

export async function updateFeatureModelGroups(
  preference: FeatureModelGroupsUpdate,
  scope: RoutingPreferenceScope = 'global',
): Promise<FeatureModelGroupsPreference> {
  const { data } = await axios.put<FeatureModelGroupsPreference>(
    '/api/v1/system/preferences/feature-model-groups',
    preference,
    scopeParams(scope),
  )
  return data
}

export interface ChatAssistPreference {
  enabled: boolean
}

export async function getChatAssistPreference(): Promise<ChatAssistPreference> {
  const { data } = await axios.get<ChatAssistPreference>(
    '/api/v1/system/preferences/chat-assist',
  )
  return data
}

export async function setChatAssistPreference(
  preference: ChatAssistPreference,
): Promise<ChatAssistPreference> {
  const { data } = await axios.put<ChatAssistPreference>(
    '/api/v1/system/preferences/chat-assist',
    preference,
  )
  return data
}

export interface SceneAccessHintPreference {
  enabled: boolean
}

export async function getSceneAccessHintPreference(): Promise<SceneAccessHintPreference> {
  const { data } = await axios.get<SceneAccessHintPreference>(
    '/api/v1/system/preferences/scene-access-hint',
  )
  return data
}

export async function setSceneAccessHintPreference(
  preference: SceneAccessHintPreference,
): Promise<SceneAccessHintPreference> {
  const { data } = await axios.put<SceneAccessHintPreference>(
    '/api/v1/system/preferences/scene-access-hint',
    preference,
  )
  return data
}

export type VisualGenerationStyle = 'anime' | 'realistic'

export interface VisualGenerationStylePreference {
  style: VisualGenerationStyle
}

export async function getVisualGenerationStylePreference(): Promise<VisualGenerationStylePreference> {
  const { data } = await axios.get<VisualGenerationStylePreference>(
    '/api/v1/system/preferences/visual-generation-style',
  )
  return data
}

export async function setVisualGenerationStylePreference(
  preference: VisualGenerationStylePreference,
): Promise<VisualGenerationStylePreference> {
  const { data } = await axios.put<VisualGenerationStylePreference>(
    '/api/v1/system/preferences/visual-generation-style',
    preference,
  )
  return data
}

export interface NsfwModeTarget {
  llm_provider_id: string
  llm_model_id: string
  image_profile_id: string
}

export interface NsfwModePreference {
  active: boolean
  configured: boolean
  locked: boolean
  ttl_seconds: number
  last_activity_at: string | null
  expires_at: string | null
  target: NsfwModeTarget | null
}

export interface NsfwModePreferenceUpdate {
  active: boolean
}

export interface NsfwModeTargetPreference {
  configured: boolean
  locked: boolean
  target: NsfwModeTarget | null
}

export async function getNsfwModePreference(): Promise<NsfwModePreference> {
  const { data } = await axios.get<NsfwModePreference>(
    '/api/v1/system/preferences/nsfw-mode',
  )
  return data
}

export async function setNsfwModePreference(
  preference: NsfwModePreferenceUpdate,
): Promise<NsfwModePreference> {
  const { data } = await axios.put<NsfwModePreference>(
    '/api/v1/system/preferences/nsfw-mode',
    preference,
  )
  return data
}

export async function getAdminNsfwModeTarget(): Promise<NsfwModeTargetPreference> {
  const { data } = await axios.get<NsfwModeTargetPreference>(
    '/api/v1/admin/system/preferences/nsfw-mode-target',
  )
  return data
}

export async function setAdminNsfwModeTarget(
  target: NsfwModeTarget,
): Promise<NsfwModeTargetPreference> {
  const { data } = await axios.put<NsfwModeTargetPreference>(
    '/api/v1/admin/system/preferences/nsfw-mode-target',
    target,
  )
  return data
}

/** Per-character feature_models — same shape as the global pref but
 * scoped to one character. Persisted on the character row, not the
 * global preferences table. Includes the `chat` feature key so one
 * character can override the global chat route.
 */
export interface CharacterFeatureModelEntry {
  feature_key: string
  provider_id: string | null
  model_id: string | null
}

export interface CharacterFeatureModelsResponse {
  /** provider/model overrides keyed by feature_key, same as the global
   * pref. Missing entries fall back through global feature-models →
   * active_model → container default. */
  overrides: Record<string, CharacterFeatureModelEntry>
  known_keys: string[]
  labels: Record<string, string>
}

export async function getCharacterFeatureModelPreferences(
  characterId: string,
): Promise<CharacterFeatureModelsResponse> {
  const { data } = await axios.get<CharacterFeatureModelsResponse>(
    `/api/v1/characters/${encodeURIComponent(characterId)}/preferences/feature-models`,
  )
  return data
}

export async function setCharacterFeatureModelPreferences(
  characterId: string,
  overrides: Record<string, CharacterFeatureModelEntry>,
): Promise<CharacterFeatureModelsResponse> {
  const { data } = await axios.put<CharacterFeatureModelsResponse>(
    `/api/v1/characters/${encodeURIComponent(characterId)}/preferences/feature-models`,
    { overrides },
  )
  return data
}

// ----------------------------------------------------------------------
// Image-profile routing (parallel to LLM feature-models above).
//
// Profiles bundle an image backend (comfyui / openai) + its kind-specific
// config (checkpoint, workflow, quality, ...) behind a single id. The
// resolver fall-through chain is identical to the LLM one — per-character
// override → global per-feature override → global active profile → first
// registered profile.
// ----------------------------------------------------------------------

export interface ImageProfileSummary {
  id: string
  label: string
  /** `comfyui` or `openai`. The UI uses it only for the tooltip hint;
   *  backend doesn't need it once the profile is built. */
  kind: string
}

export async function listImageProfiles(): Promise<ImageProfileSummary[]> {
  const { data } = await axios.get<ImageProfileSummary[]>(
    '/api/v1/system/image-profiles',
  )
  return data
}

export interface ActiveImageProfilePreference {
  profile_id: string | null
}

export async function getActiveImageProfilePreference(
  scope: RoutingPreferenceScope = 'global',
): Promise<ActiveImageProfilePreference> {
  const { data } = await axios.get<ActiveImageProfilePreference>(
    '/api/v1/system/preferences/active-image-profile',
    scopeParams(scope),
  )
  return data
}

export async function setActiveImageProfilePreference(
  preference: ActiveImageProfilePreference,
  scope: RoutingPreferenceScope = 'global',
): Promise<ActiveImageProfilePreference> {
  const { data } = await axios.put<ActiveImageProfilePreference>(
    '/api/v1/system/preferences/active-image-profile',
    preference,
    scopeParams(scope),
  )
  return data
}

export interface ImageFeatureEntry {
  profile_id: string | null
}

export interface ImageFeatureProfilesPreference {
  overrides: Record<string, ImageFeatureEntry>
  known_keys: string[]
  labels: Record<string, string>
}

export async function getImageFeatureProfilePreferences(
  scope: RoutingPreferenceScope = 'global',
): Promise<ImageFeatureProfilesPreference> {
  const { data } = await axios.get<ImageFeatureProfilesPreference>(
    '/api/v1/system/preferences/image-feature-profiles',
    scopeParams(scope),
  )
  return data
}

export async function setImageFeatureProfilePreferences(
  preference: ImageFeatureProfilesPreference,
  scope: RoutingPreferenceScope = 'global',
): Promise<ImageFeatureProfilesPreference> {
  const { data } = await axios.put<ImageFeatureProfilesPreference>(
    '/api/v1/system/preferences/image-feature-profiles',
    preference,
    scopeParams(scope),
  )
  return data
}

/** Per-character image-profile entry — mirrors
 * :class:`CharacterFeatureModelEntry` for the image side. */
export interface CharacterImageProfileEntry {
  feature_key: string
  profile_id: string | null
}

export interface CharacterImageProfilesResponse {
  overrides: Record<string, CharacterImageProfileEntry>
  known_keys: string[]
  labels: Record<string, string>
}

export async function getCharacterImageProfilePreferences(
  characterId: string,
): Promise<CharacterImageProfilesResponse> {
  const { data } = await axios.get<CharacterImageProfilesResponse>(
    `/api/v1/characters/${encodeURIComponent(characterId)}/preferences/image-profiles`,
  )
  return data
}

export async function setCharacterImageProfilePreferences(
  characterId: string,
  overrides: Record<string, CharacterImageProfileEntry>,
): Promise<CharacterImageProfilesResponse> {
  const { data } = await axios.put<CharacterImageProfilesResponse>(
    `/api/v1/characters/${encodeURIComponent(characterId)}/preferences/image-profiles`,
    { overrides },
  )
  return data
}

// ----------------------------------------------------------------------
// Video-profile routing (parallel to image-profile above).
// ----------------------------------------------------------------------

export interface VideoProfileSummary {
  id: string
  label: string
  /** Currently always ``comfyui_wan22``; reserved for future backends. */
  kind: string
}

export async function listVideoProfiles(): Promise<VideoProfileSummary[]> {
  const { data } = await axios.get<VideoProfileSummary[]>(
    '/api/v1/system/video-profiles',
  )
  return data
}

export interface ActiveVideoProfilePreference {
  profile_id: string | null
}

export async function getActiveVideoProfilePreference(
  scope: RoutingPreferenceScope = 'global',
): Promise<ActiveVideoProfilePreference> {
  const { data } = await axios.get<ActiveVideoProfilePreference>(
    '/api/v1/system/preferences/active-video-profile',
    scopeParams(scope),
  )
  return data
}

export async function setActiveVideoProfilePreference(
  preference: ActiveVideoProfilePreference,
  scope: RoutingPreferenceScope = 'global',
): Promise<ActiveVideoProfilePreference> {
  const { data } = await axios.put<ActiveVideoProfilePreference>(
    '/api/v1/system/preferences/active-video-profile',
    preference,
    scopeParams(scope),
  )
  return data
}

export interface VideoFeatureEntry {
  profile_id: string | null
}

export interface VideoFeatureProfilesPreference {
  overrides: Record<string, VideoFeatureEntry>
  known_keys: string[]
  labels: Record<string, string>
}

export async function getVideoFeatureProfilePreferences(
  scope: RoutingPreferenceScope = 'global',
): Promise<VideoFeatureProfilesPreference> {
  const { data } = await axios.get<VideoFeatureProfilesPreference>(
    '/api/v1/system/preferences/video-feature-profiles',
    scopeParams(scope),
  )
  return data
}

export async function setVideoFeatureProfilePreferences(
  preference: VideoFeatureProfilesPreference,
  scope: RoutingPreferenceScope = 'global',
): Promise<VideoFeatureProfilesPreference> {
  const { data } = await axios.put<VideoFeatureProfilesPreference>(
    '/api/v1/system/preferences/video-feature-profiles',
    preference,
    scopeParams(scope),
  )
  return data
}

export interface CharacterVideoProfileEntry {
  feature_key: string
  profile_id: string | null
}

export interface CharacterVideoProfilesResponse {
  overrides: Record<string, CharacterVideoProfileEntry>
  known_keys: string[]
  labels: Record<string, string>
}

export async function getCharacterVideoProfilePreferences(
  characterId: string,
): Promise<CharacterVideoProfilesResponse> {
  const { data } = await axios.get<CharacterVideoProfilesResponse>(
    `/api/v1/characters/${encodeURIComponent(characterId)}/preferences/video-profiles`,
  )
  return data
}

export async function setCharacterVideoProfilePreferences(
  characterId: string,
  overrides: Record<string, CharacterVideoProfileEntry>,
): Promise<CharacterVideoProfilesResponse> {
  const { data } = await axios.put<CharacterVideoProfilesResponse>(
    `/api/v1/characters/${encodeURIComponent(characterId)}/preferences/video-profiles`,
    { overrides },
  )
  return data
}
