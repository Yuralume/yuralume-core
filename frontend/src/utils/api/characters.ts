import axios from 'axios'
import { getStoredToken } from '@/composables/useAuth'
import type {
  Character,
  CharacterDisposition,
  CharacterEncounter,
  CharacterEncounterTickResult,
  CharacterPersonalityType,
  CharacterVisualGenerationStyle,
  CharacterRelationship,
  VisualSubjectType,
  CreateCharacterRequest,
  InitialRelationshipPayload,
  InitialRelationshipSafeUserProfile,
  ProactiveRhythm,
  UpdateCharacterRequest,
} from '@/types/character'
import type { ChatSurface, StageAccessVerdict } from '@/types/chat'

const BASE = '/api/v1/characters'

export async function listCharacters(): Promise<Character[]> {
  const { data } = await axios.get<Character[]>(BASE)
  return data
}

export async function getCharacter(id: string): Promise<Character> {
  const { data } = await axios.get<Character>(`${BASE}/${id}`)
  return data
}

export async function getStageAccess(
  id: string,
  surface: ChatSurface = 'web_stage',
): Promise<StageAccessVerdict> {
  const { data } = await axios.get<StageAccessVerdict>(
    `${BASE}/${id}/stage-access`,
    { params: { surface } },
  )
  return data
}

export async function createCharacter(req: CreateCharacterRequest): Promise<Character> {
  const { data } = await axios.post<Character>(BASE, req)
  return data
}

export interface CharacterCreationDraftPayload {
  name?: string
  summary?: string
  personality?: string[]
  interests?: string[]
  speaking_style?: string
  boundaries?: string[]
  aspirations?: string[]
  personality_type_code?: string
  personality_type_rationale?: string
}

export interface CharacterCreationIntakeQuestion {
  field: string
  question: string
  suggestions: string[]
}

export interface CharacterCreationIntakeWarning {
  kind: string
  message: string
  blocking: boolean
}

export interface CharacterCreationIntakeAnalysis {
  can_create: boolean
  missing_required: string[]
  questions: CharacterCreationIntakeQuestion[]
  normalized_relationship: InitialRelationshipPayload
  normalized_user_profile: InitialRelationshipSafeUserProfile
  warnings: CharacterCreationIntakeWarning[]
}

export async function analyzeCharacterCreationIntake(options: {
  character_draft: CharacterCreationDraftPayload
  relationship?: InitialRelationshipPayload | null
  current_locale?: string
  round_index?: number
}): Promise<CharacterCreationIntakeAnalysis> {
  const { data } = await axios.post<CharacterCreationIntakeAnalysis>(
    `${BASE}/creation-intake/analyze`,
    {
      character_draft: options.character_draft,
      relationship: options.relationship ?? {},
      current_locale: options.current_locale ?? '',
      round_index: options.round_index ?? 0,
    },
  )
  return data
}

export async function updateCharacter(id: string, req: UpdateCharacterRequest): Promise<Character> {
  const { data } = await axios.patch<Character>(`${BASE}/${id}`, req)
  return data
}

export async function updateCharacterProactiveRhythm(
  id: string,
  rhythm: ProactiveRhythm,
): Promise<Character> {
  const { data } = await axios.patch<Character>(
    `${BASE}/${id}/proactive-rhythm`,
    { rhythm },
  )
  return data
}

export async function deleteCharacter(id: string): Promise<void> {
  await axios.delete(`${BASE}/${id}`)
}

export interface ResetCharacterRequest {
  memories?: boolean
  conversations?: boolean
  state_history?: boolean
}

export interface ResetCharacterResponse {
  character_id: string
  memories_deleted: number
  conversations_deleted: number
  state_history_deleted: number
}

export async function uploadCharacterImage(
  characterId: string,
  file: File,
): Promise<Character> {
  const form = new FormData()
  form.append('image', file)
  const { data } = await axios.post<Character>(
    `${BASE}/${characterId}/images`,
    form,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  )
  return data
}

export async function deleteCharacterImage(
  characterId: string,
  url: string,
): Promise<Character> {
  const { data } = await axios.delete<Character>(
    `${BASE}/${characterId}/images`,
    { params: { url } },
  )
  return data
}

export async function reorderCharacterImages(
  characterId: string,
  order: string[],
): Promise<Character> {
  const { data } = await axios.put<Character>(
    `${BASE}/${characterId}/images/order`,
    { order },
  )
  return data
}

export type PortraitAspect = 'portrait' | 'landscape' | 'square'

export async function generateCharacterPortrait(
  characterId: string,
  positive: string,
  aspect: PortraitAspect = 'portrait',
): Promise<Character> {
  const { data } = await axios.post<Character>(
    `${BASE}/${characterId}/images/generate`,
    { positive, aspect },
  )
  return data
}

export interface GenerateCandidatesResponse {
  character_id: string
  candidates: string[]
}

export async function generatePortraitCandidates(
  characterId: string,
  positive: string,
  aspect: PortraitAspect = 'portrait',
  count: number = 4,
): Promise<GenerateCandidatesResponse> {
  const { data } = await axios.post<GenerateCandidatesResponse>(
    `${BASE}/${characterId}/images/candidates`,
    { positive, aspect, count },
  )
  return data
}

export async function commitPortraitCandidates(
  characterId: string,
  keepUrls: string[],
  albumUrls: string[] = [],
): Promise<Character> {
  const { data } = await axios.post<Character>(
    `${BASE}/${characterId}/images/candidates/commit`,
    { keep_urls: keepUrls, album_urls: albumUrls },
  )
  return data
}

export async function listAvailableLoras(characterId: string): Promise<string[]> {
  const { data } = await axios.get<string[]>(
    `${BASE}/${characterId}/loras/available`,
  )
  return data
}

export async function uploadCharacterLora(
  characterId: string,
  file: File,
  strength: number = 1.0,
): Promise<Character> {
  const form = new FormData()
  form.append('lora', file)
  form.append('strength', String(strength))
  const { data } = await axios.post<Character>(
    `${BASE}/${characterId}/loras`,
    form,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  )
  return data
}

export async function attachExistingLora(
  characterId: string,
  name: string,
  strength: number = 1.0,
): Promise<Character> {
  const { data } = await axios.post<Character>(
    `${BASE}/${characterId}/loras/attach`,
    { name, strength },
  )
  return data
}

export async function setCharacterLoraStrength(
  characterId: string,
  name: string,
  strength: number,
): Promise<Character> {
  const { data } = await axios.patch<Character>(
    `${BASE}/${characterId}/loras`,
    { name, strength },
  )
  return data
}

export async function removeCharacterLora(
  characterId: string,
  name: string,
): Promise<Character> {
  const { data } = await axios.delete<Character>(
    `${BASE}/${characterId}/loras`,
    { params: { name } },
  )
  return data
}

export async function resetCharacterData(
  id: string,
  req: ResetCharacterRequest,
): Promise<ResetCharacterResponse> {
  const { data } = await axios.post<ResetCharacterResponse>(
    `${BASE}/${id}/reset`,
    req,
  )
  return data
}

export interface CharacterDraftCompanion {
  /**
   * 新生成的草稿同伴 ``id`` 一定是 ``null`` —— 後端只在實際 persist
   * 時才配發 UUID。前端在拼合到 CharacterCompanion[] 時可以維持
   * ``null``，再連同其他欄位整批送到 PATCH/POST 即可。
   */
  id: string | null
  name: string
  role: string
  brief_profile: string
  personality_sketch: string[]
  relationship_snippet: string
}

export interface CharacterDraftNameCandidate {
  name: string
  rationale: string
}

export interface CharacterDraft {
  name: string
  name_candidates: CharacterDraftNameCandidate[]
  summary: string
  personality: string[]
  interests: string[]
  speaking_style: string
  boundaries: string[]
  aspirations: string[]
  appearance: string
  gender_identity: string
  third_person_pronoun: string
  visual_gender_presentation: string
  visual_subject_type: VisualSubjectType
  visual_generation_style: CharacterVisualGenerationStyle
  date_of_birth: string | null
  world_frame: string
  personality_type: CharacterPersonalityType
  companions: CharacterDraftCompanion[]
}

export async function generateCompanions(
  characterId: string,
  options: { hint?: string; count?: number } = {},
): Promise<CharacterDraftCompanion[]> {
  const { data } = await axios.post<{ suggestions: CharacterDraftCompanion[] }>(
    `${BASE}/${characterId}/companions/generate`,
    {
      hint: options.hint ?? null,
      count: options.count ?? 3,
    },
  )
  return data.suggestions ?? []
}

export async function generateCharacterDraft(options: {
  prompt?: string
  image?: File | null
}): Promise<CharacterDraft> {
  const form = new FormData()
  if (options.prompt && options.prompt.trim()) {
    form.append('prompt', options.prompt)
  }
  if (options.image) {
    form.append('image', options.image)
  }
  const { data } = await axios.post<CharacterDraft>(
    `${BASE}/draft`,
    form,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  )
  return data
}

/**
 * 觸發角色卡（`.lumecard`）原生下載。後端只打包可攜的 A 層設定 + 同捆
 * arc template + 舞台圖；B 層路由與 C 層 runtime 累積一律不帶。
 *
 * 這裡刻意不用 XHR/axios blob：瀏覽器擴充或 inspector 腳本常會對 blob
 * XHR 讀 `responseText` 而丟 InvalidStateError；同時 object URL 在 HTTP
 * 部署會形成 `blob:http://...` 下載警告。原生下載交給後端的
 * Content-Disposition 決定檔名（含 RFC 5987 `filename*` CJK 檔名）。
 */
export function downloadCharacterCard(
  characterId: string,
  options: {
    includeArcTemplateIds?: string[]
    includeArcSeriesIds?: string[]
  } = {},
): void {
  const url = buildCharacterCardDownloadUrl(characterId, options)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.rel = 'noopener'
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

function buildCharacterCardDownloadUrl(
  characterId: string,
  options: {
    includeArcTemplateIds?: string[]
    includeArcSeriesIds?: string[]
  } = {},
): string {
  const params = new URLSearchParams()
  for (const templateId of options.includeArcTemplateIds ?? []) {
    params.append('include_arc_template_ids', templateId)
  }
  for (const seriesId of options.includeArcSeriesIds ?? []) {
    params.append('include_arc_series_ids', seriesId)
  }
  const token = getStoredToken()
  if (token) {
    // Native downloads cannot send Authorization headers; backend auth
    // already accepts this GET-only fallback for browser APIs with the
    // same limitation.
    params.set('access_token', token)
  }
  const query = params.toString()
  return `${BASE}/${encodeURIComponent(characterId)}/card${query ? `?${query}` : ''}`
}

export interface ImportCardResult {
  character: Character
  landed_arc_template_ids: string[]
  landed_arc_series_ids: string[]
}

/**
 * 匯入角色卡（`.lumecard`）。後端會建立一個全新角色（A 層設定 + 重傳
 * 舞台圖 + 落地同捆 arc template，id 衝突自動 remap）；B/C 層一律歸零，
 * 卡片本身不攜帶關係 runtime；confirm request 可附匯入者確認的本地起始關係。
 * 回傳新角色與實際落地的 template id 清單。
 */
export async function importCharacterCard(
  file: File,
  options: {
    translate?: boolean
    initialRelationship?: InitialRelationshipPayload | null
  } = {},
): Promise<ImportCardResult> {
  const form = new FormData()
  form.append('card', file)
  if (options.translate) {
    form.append('translate', 'true')
  }
  if (options.initialRelationship) {
    form.append('initial_relationship', JSON.stringify(options.initialRelationship))
  }
  const { data } = await axios.post<ImportCardResult>(
    `${BASE}/import`,
    form,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  )
  return data
}

export interface CharacterCardPreviewCompanion {
  name: string
  role: string
}

export interface CharacterCardPreview {
  pack_id: string | null
  title: string
  author: string
  description: string
  tags: string[]
  note: string
  name: string
  summary: string
  personality: string[]
  interests: string[]
  speaking_style: string
  boundaries: string[]
  aspirations: string[]
  appearance: string
  gender_identity: string
  third_person_pronoun: string
  visual_gender_presentation: string
  visual_subject_type: VisualSubjectType
  date_of_birth: string | null
  disposition: CharacterDisposition
  personality_type: CharacterPersonalityType
  world_frame: string
  world_awareness_enabled: boolean
  world_topics: string[]
  subscribed_categories: string[]
  excluded_topics: string[]
  proactive_enabled: boolean
  proactive_daily_limit: number
  proactive_cooldown_minutes: number
  accepts_web_proactive: boolean
  feed_daily_limit: number
  companions: CharacterCardPreviewCompanion[]
  has_main_arc: boolean
  bundled_arc_template_count: number
  bundled_arc_titles: string[]
  has_arc_series: boolean
  bundled_arc_series_count: number
  bundled_arc_series_titles: string[]
  bundled_arc_series_member_count: number
  stage_image_count: number
  image_urls: string[]
  /** 'lumecard' for the native path (and bundled packs), 'sillytavern' when
   *  the upload was a converted SillyTavern card. */
  source_format?: string
  /** Stable markers for SillyTavern fields the import dropped
   *  ('character_book' / 'greetings' / 'extra_assets'); empty otherwise. */
  dropped_fields?: string[]
  /** Neutral rewrite of a SillyTavern scenario used to pre-fill the
   *  initial-relationship wizard. Never auto-applied. */
  suggested_known_context?: string
}

export interface CharacterCardPackSummary extends CharacterCardPreview {
  pack_id: string
}

export async function previewCharacterCard(
  file: File,
  options: { translate?: boolean } = {},
): Promise<CharacterCardPreview> {
  const form = new FormData()
  form.append('card', file)
  if (options.translate) {
    form.append('translate', 'true')
  }
  const { data } = await axios.post<CharacterCardPreview>(
    `${BASE}/card/preview`,
    form,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  )
  return withCharacterCardImageAccess(data)
}

/** 列出隨 repo 出貨的角色卡市集 packs（`src/kokoro_link/data/character_cards/`）。 */
export async function listCharacterCards(): Promise<CharacterCardPackSummary[]> {
  const { data } = await axios.get<CharacterCardPackSummary[]>('/api/v1/character-cards')
  return data.map(withCharacterCardImageAccess)
}

export async function previewCharacterCardPack(
  packId: string,
  options: { translate?: boolean } = {},
): Promise<CharacterCardPreview> {
  const { data } = await axios.post<CharacterCardPreview>(
    `/api/v1/character-cards/${encodeURIComponent(packId)}/preview`,
    null,
    { params: { translate: options.translate || undefined } },
  )
  return withCharacterCardImageAccess(data)
}

/**
 * 安裝一張市集角色卡 —— 後端走與手動匯入相同的路徑，建立一個全新角色
 * （A 層設定 + 落地同捆 arc template，B/C 層歸零，可附本地起始關係 seed）。
 */
export async function installCharacterCard(
  packId: string,
  options: {
    translate?: boolean
    initialRelationship?: InitialRelationshipPayload | null
  } = {},
): Promise<ImportCardResult> {
  const body = options.initialRelationship
    ? {
        translate: Boolean(options.translate),
        initial_relationship: options.initialRelationship,
      }
    : null
  const { data } = await axios.post<ImportCardResult>(
    `/api/v1/character-cards/${encodeURIComponent(packId)}/install`,
    body,
    {
      params: body ? undefined : { translate: options.translate || undefined },
    },
  )
  return data
}

function withCharacterCardImageAccess<T extends CharacterCardPreview>(card: T): T {
  return {
    ...card,
    image_urls: card.image_urls.map(withProtectedCharacterCardImageToken),
  }
}

function withProtectedCharacterCardImageToken(url: string): string {
  const token = getStoredToken()
  if (!token || !url.startsWith('/api/v1/character-cards/')) {
    return url
  }
  if (/[?&]access_token=/.test(url)) {
    return url
  }
  const separator = url.includes('?') ? '&' : '?'
  return `${url}${separator}access_token=${encodeURIComponent(token)}`
}

export interface CreateRelationshipPayload {
  target_character_id: string
  relationship_label?: string
  how_a_sees_b?: string
  how_b_sees_a?: string
  peer_profile_seed?: PeerProfileSeedPayload | null
}

export interface PeerProfileSeedPayload {
  summary?: string
  occupation?: string
  haunts?: string[]
  habits?: string[]
  relationship_note?: string
  shared_activities?: string[]
}

export interface UpdateRelationshipPayload {
  enabled?: boolean | null
  relationship_label?: string | null
  how_a_sees_b?: string | null
  how_b_sees_a?: string | null
  affection_a_to_b?: number | null
  affection_b_to_a?: number | null
  trust_a_to_b?: number | null
  trust_b_to_a?: number | null
}

export async function listCharacterRelationships(
  characterId: string,
): Promise<CharacterRelationship[]> {
  const { data } = await axios.get<CharacterRelationship[]>(
    `${BASE}/${characterId}/relationships`,
  )
  return data
}

export async function createCharacterRelationship(
  characterId: string,
  payload: CreateRelationshipPayload,
): Promise<CharacterRelationship> {
  const { data } = await axios.post<CharacterRelationship>(
    `${BASE}/${characterId}/relationships`,
    payload,
  )
  return data
}

export async function updateCharacterRelationship(
  relationshipId: string,
  payload: UpdateRelationshipPayload,
): Promise<CharacterRelationship> {
  const { data } = await axios.patch<CharacterRelationship>(
    `/api/v1/character-relationships/${relationshipId}`,
    payload,
  )
  return data
}

export async function listCharacterEncounters(
  characterId: string,
): Promise<CharacterEncounter[]> {
  const { data } = await axios.get<CharacterEncounter[]>(
    `${BASE}/${characterId}/encounters`,
  )
  return data
}

export async function tickCharacterEncounters(): Promise<CharacterEncounterTickResult> {
  const { data } = await axios.post<CharacterEncounterTickResult>(
    '/api/v1/admin/character-encounters/tick',
  )
  return data
}
