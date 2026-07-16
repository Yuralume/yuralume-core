export interface CharacterState {
  emotion: string
  affection: number
  fatigue: number
  trust: number
  energy: number
  current_intent?: string | null
}

export interface CharacterLora {
  name: string
  strength: number
}

/**
 * 私人 NPC 同伴 —— 角色生活圈裡的配角（同事、室友、家人…）。
 * NPC 不會自己跑 surface（不發貼文、不主動對話），只是讓角色的行程、
 * 記憶、聊天提到「跟誰一起」變得自然。
 *
 * ``id`` 在新增草稿時可以是 ``null``，建立／更新呼叫時後端會自動
 * 配發 UUID；既有同伴回來時 ``id`` 永遠是 string。
 */
export interface CharacterCompanion {
  id: string | null
  name: string
  role: string
  brief_profile: string
  personality_sketch: string[]
  relationship_snippet: string
}

export interface CharacterRelationship {
  id: string
  character_a_id: string
  character_b_id: string
  enabled: boolean
  relationship_label: string
  how_a_sees_b: string
  how_b_sees_a: string
  affection_a_to_b: number
  affection_b_to_a: number
  trust_a_to_b: number
  trust_b_to_a: number
  last_interaction_at: string | null
  created_at: string
  updated_at: string
}

export interface EncounterLine {
  speaker_character_id: string
  text: string
}

export interface CharacterEncounter {
  id: string
  relationship_id: string
  character_a_id: string
  character_b_id: string
  scheduled_for: string
  location: string
  status: 'planned' | 'running' | 'completed' | 'failed' | string
  trigger_reason: string
  max_turns: number
  transcript: EncounterLine[]
  summary_for_a: string
  summary_for_b: string
  memory_ids: string[]
  last_error: string | null
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
}

export interface CharacterEncounterTickResult {
  planned: number
  completed: number
  failed: number
  planned_ids: string[]
  completed_ids: string[]
  failed_ids: string[]
}

/**
 * Per-character TTS override. Empty-string fields fall back to the
 * global ``KOKORO_TTS_*`` env values at synthesis time, so a partial
 * profile is fine. ``enabled=false`` disables TTS for this character
 * even when global is configured.
 */
/**
 * 角色的內在動機傾向四維 qualitative band（low / medium / high）。
 * 全 medium = 等同「沒設定」，後端 prompt builder 會自動跳過渲染。
 *
 * - ``self_centeredness`` — 自我中心度（高=愛聊自己 / 低=偏好問對方）
 * - ``candor``           — 直言程度（高=有意見就直說 / 低=傾向附和）
 * - ``sharing_drive``    — 分享慾（高=常想找人說話 / 低=沒事不主動）
 * - ``associativeness``  — 聯想力（高=愛翻舊帳 / 低=就事論事）
 *
 * **禁止**在前端用這些欄位寫 if/else 條件 —— 它們只是給後端塞進 prompt
 * 的事實層，不是行為開關。
 */
export type DispositionBand = 'low' | 'medium' | 'high'

export interface CharacterDisposition {
  self_centeredness: DispositionBand
  candor: DispositionBand
  sharing_drive: DispositionBand
  associativeness: DispositionBand
}

/**
 * HUMANIZATION_ROADMAP §3.6 — operator-facing dialogue pace knob.
 *
 * ``""`` = unset (no prompt injection, legacy behaviour).
 * The backend collapses unknown values to ``""``.
 */
export type OperatorPacePreference = '' | 'more_active' | 'balanced' | 'more_quiet'

export type ProactiveRhythm = 'quiet' | 'balanced' | 'lively'

export type CharacterPersonalityTypeCode =
  | ''
  | 'INTJ' | 'INTP' | 'ENTJ' | 'ENTP'
  | 'INFJ' | 'INFP' | 'ENFJ' | 'ENFP'
  | 'ISTJ' | 'ISFJ' | 'ESTJ' | 'ESFJ'
  | 'ISTP' | 'ISFP' | 'ESTP' | 'ESFP'

export type CharacterPersonalityTypeSource = 'unset' | 'user_explicit' | 'llm_inferred'

export type VisualSubjectType =
  | 'auto'
  | 'human'
  | 'animal'
  | 'anthropomorphic'
  | 'creature'
  | 'object'

export type CharacterVisualGenerationStyle = '' | 'anime' | 'realistic'

export interface CharacterPersonalityType {
  system: 'mbti_16'
  code: CharacterPersonalityTypeCode
  source: CharacterPersonalityTypeSource
  confidence: number
  rationale: string
  consistency_notes: string[]
}

export type ScheduleInvolvementPolicy =
  | 'none'
  | 'mention_only'
  | 'invite_required'
  | 'shared_allowed'

export interface InitialRelationshipSafeUserProfile {
  name?: string
  nickname?: string
  occupation?: string
  company_or_school?: string
  interests?: string[]
  routine?: string
  life_goals?: string[]
}

export interface InitialRelationshipPayload {
  relationship_label?: string
  known_context?: string
  living_arrangement?: string
  user_address_name?: string
  character_address_name?: string
  tone_distance?: string
  familiarity_boundary?: string
  schedule_involvement_policy?: ScheduleInvolvementPolicy
  proactive_permission?: boolean
  proactive_cadence_hint?: string
  user_profile_notes?: string
  confirmed_by_user?: boolean
  safe_user_profile?: InitialRelationshipSafeUserProfile
}

export interface VoiceProfile {
  enabled: boolean
  voice_id: string
  ref_audio_path: string
  prompt_text: string
  prompt_lang: string
  /**
   * Pre-TTS LLM dubbing target ('zh' / 'ja' / ...). Empty inherits
   * the global ``KOKORO_TTS_TRANSLATE_TARGET_LANG``. Use ``-`` to
   * explicitly disable translation for this character even when the
   * global default has one.
   */
  translate_target_lang: string
  gpt_weights_path: string
  sovits_weights_path: string
}

export interface Character {
  id: string
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
  visual_generation_style: CharacterVisualGenerationStyle
  /**
   * ISO 8601 ``YYYY-MM-DD`` 字串，``null`` 表示未設定。後端會用這個
   * 即時推算年齡、星座、距離下一次生日的天數，並決定是否觸發生日
   * 限動或在提示中加入慶生指引。
   */
  date_of_birth: string | null
  image_urls: string[]
  allowed_tools: string[]
  loras: CharacterLora[]
  state: CharacterState
  proactive_enabled: boolean
  proactive_daily_limit: number
  proactive_cooldown_minutes: number
  proactive_rhythm: ProactiveRhythm
  feed_daily_limit: number
  world_awareness_enabled: boolean
  world_topics: string[]
  /**
   * RSS category allow-list. Empty = consider every enabled source.
   * Coarse pre-filter for the per-character event inbox curator;
   * embedding similarity does the fine matching after.
   */
  subscribed_categories: string[]
  /**
   * Free-form topics this character avoids (e.g. "政治", "醜聞").
   * Each entry is embedded once and the curator drops candidate
   * events whose embedding cosine to any excluded vector exceeds an
   * exclusion threshold.
   */
  excluded_topics: string[]
  world_frame: string
  accepts_web_proactive: boolean
  unread_proactive_count: number
  /**
   * Number of unread LumeGram comment replies the character has
   * posted at the user since the last time the overlay was opened.
   * Drives the red dot on the StagePage launcher icon. Reset by the
   * existing ``POST /characters/{id}/feed/seen`` endpoint.
   */
  unread_feed_reply_count: number
  /**
   * Bound arc-template id (Phase 2 of SCENE_BEAT_PLAN). When set, the
   * next new arc materialises this YAML template instead of asking the
   * LLM to plan one. ``null`` = LLM planning (legacy behaviour).
   */
  arc_template_id: string | null
  arc_series_id: string | null
  voice_profile: VoiceProfile | null
  /**
   * 私人 NPC 同伴清單。空 list = 沒有同伴（預設）。
   */
  companions: CharacterCompanion[]
  /**
   * 內在動機傾向四維。後端總是回傳完整四個欄位（缺省全 medium）。
   * 詳見 :type:`CharacterDisposition`。
   */
  disposition: CharacterDisposition
  personality_type: CharacterPersonalityType
  /**
   * HUMANIZATION_ROADMAP §3.6 — operator dialogue-pace knob.
   * ``""`` 等同未設定（沒有 prompt 注入）。
   */
  operator_pace_preference: OperatorPacePreference
}

export interface CreateCharacterRequest {
  name: string
  summary?: string
  personality?: string[]
  interests?: string[]
  speaking_style?: string
  boundaries?: string[]
  aspirations?: string[]
  appearance?: string
  gender_identity?: string
  third_person_pronoun?: string
  visual_gender_presentation?: string
  visual_subject_type?: VisualSubjectType
  visual_generation_style?: CharacterVisualGenerationStyle
  /** ISO ``YYYY-MM-DD``；省略 / ``null`` 表示不設定生日。 */
  date_of_birth?: string | null
  allowed_tools?: string[]
  initial_state?: Partial<CharacterState>
  proactive_enabled?: boolean
  proactive_daily_limit?: number
  proactive_cooldown_minutes?: number
  feed_daily_limit?: number
  world_awareness_enabled?: boolean
  world_topics?: string[]
  subscribed_categories?: string[]
  excluded_topics?: string[]
  world_frame?: string
  accepts_web_proactive?: boolean
  arc_template_id?: string | null
  arc_series_id?: string | null
  /** 建立時就可以一併帶入同伴；省略 = 沒有同伴。 */
  companions?: CharacterCompanion[]
  /**
   * 內在動機傾向四維。省略 / 全 medium = 沒設定。
   */
  disposition?: CharacterDisposition
  personality_type?: CharacterPersonalityType
  initial_relationship?: InitialRelationshipPayload | null
}

export interface UpdateCharacterRequest {
  name?: string | null
  summary?: string | null
  personality?: string[] | null
  interests?: string[] | null
  speaking_style?: string | null
  boundaries?: string[] | null
  aspirations?: string[] | null
  appearance?: string | null
  gender_identity?: string | null
  third_person_pronoun?: string | null
  visual_gender_presentation?: string | null
  visual_subject_type?: VisualSubjectType | null
  visual_generation_style?: CharacterVisualGenerationStyle | null
  /**
   * Tri-state — *omit* 不更新；``null`` 清除回未設定；``"YYYY-MM-DD"``
   * 設定為該日期。後端用 Pydantic v2 ``model_fields_set`` 分辨「省略」
   * 與「顯式 null」。
   */
  date_of_birth?: string | null
  allowed_tools?: string[] | null
  state?: CharacterState | null
  proactive_enabled?: boolean | null
  proactive_daily_limit?: number | null
  proactive_cooldown_minutes?: number | null
  feed_daily_limit?: number | null
  world_awareness_enabled?: boolean | null
  world_topics?: string[] | null
  subscribed_categories?: string[] | null
  excluded_topics?: string[] | null
  world_frame?: string | null
  accepts_web_proactive?: boolean | null
  /**
   * Tri-state — *omit* the property to leave alone, ``null`` to unbind
   * (clear the binding back to LLM planning), or a string id to bind.
   * Backend uses Pydantic v2 ``model_fields_set`` to distinguish
   * "field absent" from "explicit null".
   */
  arc_template_id?: string | null
  /**
   * Tri-state series binding. Omit to leave alone, ``null`` to clear,
   * or a string id to bind an authored ArcSeries.
   */
  arc_series_id?: string | null
  /**
   * Tri-state — omit to leave alone, ``null`` to clear the
   * per-character override (back to global), or a payload to set.
   */
  voice_profile?: VoiceProfile | null
  /**
   * 私人 NPC 同伴清單。``undefined`` (omit) = 不動既有；``[]`` =
   * 清空所有同伴；非空陣列 = 全替換。新增的同伴 ``id`` 給 ``null``，
   * 後端會自動配發 UUID，編輯既有同伴時保留原本的 ``id``。
   */
  companions?: CharacterCompanion[] | null
  /**
   * 內在動機傾向四維。``undefined``（omit）= 不動既有；payload = 全替換。
   * 任一欄送空字串會被後端 normalise 為 ``"medium"``。
   */
  disposition?: CharacterDisposition | null
  personality_type?: CharacterPersonalityType | null
  /**
   * HUMANIZATION_ROADMAP §3.6 對話節奏偏好。``undefined`` = 不動既有；
   * 字串覆寫；未知值會被後端 normalise 為 ``""``。
   */
  operator_pace_preference?: OperatorPacePreference | null
}
