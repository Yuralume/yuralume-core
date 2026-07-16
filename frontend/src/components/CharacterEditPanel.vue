<script setup lang="ts">
/**
 * 角色詳細編輯面板（settings tab 抽出版本）。
 *
 * 同一份元件給 PlayerSidebar 的「設定」tab 與 `/admin/characters` 共用。
 * 工具權限屬於 drift-risk 設定，只在 admin 顯示；玩家頁保存時不送
 * ``allowed_tools``，避免非 admin 入口意外改到工具開關。
 *
 * 不負責的範圍：
 *   - `world_frame`：屬於「劇情」tab／與 arc-template 一起呈現，留在 PlayerSidebar
 *   - schedule / follow-ups / story：各自有獨立 panel
 *   - per-character LLM / image / voice profile：在各自 admin page
 *
 * Emits:
 *   - ``updated(char)``: handleSave 成功後丟出新版 character
 *   - ``data-reset(char)``: 漂移急救包按鈕清完資料後丟出
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'
import type {
  Character,
  CharacterCompanion,
  CharacterPersonalityType,
  CharacterPersonalityTypeCode,
  CharacterPersonalityTypeSource,
  CharacterVisualGenerationStyle,
  CharacterState,
  UpdateCharacterRequest,
  VisualSubjectType,
} from '@/types/character'
import type { ToolDescriptor } from '@/types/tool'
import { UiButton } from '@/components/ui'
import {
  generateCompanions,
  resetCharacterData,
  updateCharacter,
} from '@/utils/api/characters'
import { listTools } from '@/utils/api/tools'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import CharacterIdentityFields from './CharacterIdentityFields.vue'
import CharacterRelationshipsPanel from './CharacterRelationshipsPanel.vue'
import CollapsibleSection from './CollapsibleSection.vue'

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const PERSONALITY_TYPE_CODES: CharacterPersonalityTypeCode[] = [
  'INTJ', 'INTP', 'ENTJ', 'ENTP',
  'INFJ', 'INFP', 'ENFJ', 'ENFP',
  'ISTJ', 'ISFJ', 'ESTJ', 'ESFJ',
  'ISTP', 'ISFP', 'ESTP', 'ESFP',
]

const props = defineProps<{
  character: Character
  // 給 CharacterRelationshipsPanel 用的全名單。沒有也能跑（會顯示「無其他角色」）。
  characters?: Character[]
  showToolSettings?: boolean
  showStateSettings?: boolean
  showAdminLinks?: boolean
  showImageTriggerInfo?: boolean
  showTechnicalHints?: boolean
}>()

const emit = defineEmits<{
  updated: [char: Character]
  'data-reset': [char: Character]
}>()

// --- Form state ---------------------------------------------------
const form = ref({
  name: '',
  summary: '',
  personality: '',
  interests: '',
  speaking_style: '',
  boundaries: '',
  aspirations: '',
  appearance: '',
  gender_identity: '',
  third_person_pronoun: '',
  visual_gender_presentation: '',
  visual_subject_type: 'auto' as VisualSubjectType,
  visual_generation_style: '' as CharacterVisualGenerationStyle,
  date_of_birth: '',
  personality_type_code: '' as CharacterPersonalityTypeCode,
  personality_type_rationale: '',
  personality_type_notes: '',
  personality_type_source: 'unset' as CharacterPersonalityTypeSource,
  allowed_tools: [] as string[],
  emotion: 'neutral',
  affection: 50,
  fatigue: 0,
  trust: 50,
  energy: 100,
})

const companions = ref<CharacterCompanion[]>([])
const companionGenLoading = ref(false)
const companionGenError = ref<string | null>(null)
const companionGenHint = ref('')
const shouldShowStateSettings = computed(() => props.showStateSettings !== false)
const shouldShowAdminLinks = computed(() => props.showAdminLinks !== false)
const shouldShowImageTriggerInfo = computed(() => props.showImageTriggerInfo !== false)
const shouldShowTechnicalHints = computed(() => props.showTechnicalHints !== false)

function emptyCompanion(): CharacterCompanion {
  return {
    id: null,
    name: '',
    role: '',
    brief_profile: '',
    personality_sketch: [],
    relationship_snippet: '',
  }
}

function addCompanion() {
  companions.value.push(emptyCompanion())
}

function removeCompanion(index: number) {
  companions.value.splice(index, 1)
}

function companionSketchText(c: CharacterCompanion): string {
  return (c.personality_sketch ?? []).join(', ')
}

function updateCompanionSketch(index: number, raw: string) {
  const items = raw.split(',').map(v => v.trim()).filter(Boolean)
  companions.value[index].personality_sketch = items
}

async function generateMoreCompanions() {
  companionGenLoading.value = true
  companionGenError.value = null
  try {
    const drafts = await generateCompanions(props.character.id, {
      hint: companionGenHint.value.trim() || undefined,
      count: 3,
    })
    const seen = new Set(companions.value.map(c => c.name).filter(Boolean))
    for (const npc of drafts) {
      if (!npc.name || seen.has(npc.name)) continue
      seen.add(npc.name)
      companions.value.push({
        id: null,
        name: npc.name,
        role: npc.role || '',
        brief_profile: npc.brief_profile || '',
        personality_sketch: npc.personality_sketch ?? [],
        relationship_snippet: npc.relationship_snippet || '',
      })
    }
    companionGenHint.value = ''
  } catch (err) {
    companionGenError.value = err instanceof Error
      ? err.message
      : t('characterCreate.errors.draftFailed')
  } finally {
    companionGenLoading.value = false
  }
}

// --- Tool catalogue ----------------------------------------------
const availableTools = ref<ToolDescriptor[]>([])
const INTERNAL_TOOL_NAMES = new Set(['echo', 'fake_image'])

const TOOL_DESCRIPTION_KEYS: Record<string, string> = {
  generate_image: 'characterEdit.tools.descriptions.generate_image',
  web_fetch: 'characterEdit.tools.descriptions.web_fetch',
  web_search: 'characterEdit.tools.descriptions.web_search',
}

onMounted(async () => {
  if (props.showToolSettings === false) return
  try {
    availableTools.value = await listTools()
  } catch {
    availableTools.value = []
  }
})

const visibleTools = computed(() =>
  availableTools.value.filter(tool => !INTERNAL_TOOL_NAMES.has(tool.name)),
)

// `web_search` only appears in `/tools` when a search provider is
// configured (Admin → provider settings → search). When it's absent we
// still surface a disabled placeholder so the capability isn't a total
// blank — admins get a pointer to where to enable it, players just see
// the explanatory copy (no admin path leaked). See WEB_SEARCH_PROVIDERS
// plan P4.
const hasWebSearchTool = computed(() =>
  visibleTools.value.some(tool => tool.name === 'web_search'),
)

function toggleTool(name: string, enabled: boolean) {
  const set = new Set(form.value.allowed_tools)
  if (enabled) set.add(name)
  else set.delete(name)
  form.value.allowed_tools = Array.from(set)
}

function toolDescription(tool: ToolDescriptor): string {
  const key = TOOL_DESCRIPTION_KEYS[tool.name]
  return key ? t(key) : tool.description
}

function normalisedAllowedToolsForSave(): string[] {
  const visible = new Set(visibleTools.value.map(tool => tool.name))
  return form.value.allowed_tools.filter((name) => {
    if (INTERNAL_TOOL_NAMES.has(name)) return false
    return visible.size === 0 || visible.has(name)
  })
}

// --- Sync from character prop -----------------------------------
function syncFromCharacter(char: Character) {
  form.value = {
    name: char.name,
    summary: char.summary,
    personality: char.personality.join(', '),
    interests: char.interests.join(', '),
    speaking_style: char.speaking_style,
    boundaries: char.boundaries.join(', '),
    aspirations: (char.aspirations ?? []).join(', '),
    appearance: char.appearance ?? '',
    gender_identity: char.gender_identity ?? '',
    third_person_pronoun: char.third_person_pronoun ?? '',
    visual_gender_presentation: char.visual_gender_presentation ?? '',
    visual_subject_type: char.visual_subject_type ?? 'auto',
    visual_generation_style: char.visual_generation_style ?? '',
    date_of_birth: char.date_of_birth ?? '',
    personality_type_code: char.personality_type.code,
    personality_type_rationale: char.personality_type.rationale,
    personality_type_notes: (char.personality_type.consistency_notes ?? []).join(', '),
    personality_type_source: char.personality_type.source,
    allowed_tools: (char.allowed_tools ?? [])
      .filter(name => !INTERNAL_TOOL_NAMES.has(name)),
    emotion: char.state.emotion,
    affection: char.state.affection,
    fatigue: char.state.fatigue,
    trust: char.state.trust,
    energy: char.state.energy,
  }
  companions.value = (char.companions ?? []).map(c => ({
    id: c.id,
    name: c.name,
    role: c.role,
    brief_profile: c.brief_profile,
    personality_sketch: [...(c.personality_sketch ?? [])],
    relationship_snippet: c.relationship_snippet,
  }))
  companionGenError.value = null
  companionGenHint.value = ''
}

watch(() => props.character, (char) => {
  syncFromCharacter(char)
}, { immediate: true })

function splitList(s: string): string[] {
  return s.split(',').map(v => v.trim()).filter(Boolean)
}

function onPersonalityTypeChanged() {
  form.value.personality_type_source = form.value.personality_type_code ? 'user_explicit' : 'unset'
  if (!form.value.personality_type_code) {
    form.value.personality_type_rationale = ''
    form.value.personality_type_notes = ''
  }
}

function buildPersonalityType(): CharacterPersonalityType {
  const code = form.value.personality_type_code
  return {
    system: 'mbti_16',
    code,
    source: code ? form.value.personality_type_source : 'unset',
    confidence: code ? 1 : 0,
    rationale: form.value.personality_type_rationale.trim(),
    consistency_notes: splitList(form.value.personality_type_notes),
  }
}

// --- Save --------------------------------------------------------
const saving = ref(false)

async function handleSave() {
  saving.value = true
  try {
    const state: CharacterState = {
      emotion: form.value.emotion,
      affection: form.value.affection,
      fatigue: form.value.fatigue,
      trust: form.value.trust,
      energy: form.value.energy,
      current_intent: props.character.state.current_intent ?? null,
    }
    const req: UpdateCharacterRequest = {
      name: form.value.name,
      summary: form.value.summary,
      personality: splitList(form.value.personality),
      interests: splitList(form.value.interests),
      speaking_style: form.value.speaking_style,
      boundaries: splitList(form.value.boundaries),
      aspirations: splitList(form.value.aspirations),
      appearance: form.value.appearance,
      gender_identity: form.value.gender_identity.trim(),
      third_person_pronoun: form.value.third_person_pronoun.trim(),
      visual_gender_presentation: form.value.visual_gender_presentation.trim(),
      visual_subject_type: form.value.visual_subject_type,
      visual_generation_style: form.value.visual_generation_style,
      date_of_birth: form.value.date_of_birth.trim() ? form.value.date_of_birth : null,
      personality_type: buildPersonalityType(),
      state,
      companions: companions.value
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
    if (props.showToolSettings !== false) {
      req.allowed_tools = normalisedAllowedToolsForSave()
    }
    const updated = await updateCharacter(props.character.id, req)
    emit('updated', updated)
  } finally {
    saving.value = false
  }
}

// --- Identity-drift rescue: clear memories / conversations -------
const resetBusy = ref<'memories' | 'conversations' | 'all' | null>(null)
const resetFeedback = ref<string | null>(null)

async function runReset(
  scope: 'memories' | 'conversations' | 'all',
  options: { memories?: boolean; conversations?: boolean; state_history?: boolean },
  confirmText: string,
) {
  if (!await confirmDialog({
    content: confirmText,
    okText: scope === 'all' ? t('common.actions.confirm') : t('common.actions.clear'),
    danger: true,
  })) return
  resetBusy.value = scope
  resetFeedback.value = null
  try {
    const res = await resetCharacterData(props.character.id, options)
    const bits: string[] = []
    if (options.memories) bits.push(t('characterEdit.reset.deleted.memories', { count: res.memories_deleted }))
    if (options.conversations) bits.push(t('characterEdit.reset.deleted.conversations', { count: res.conversations_deleted }))
    if (options.state_history) bits.push(t('characterEdit.reset.deleted.stateHistory', { count: res.state_history_deleted }))
    resetFeedback.value = t('characterEdit.reset.success', { details: bits.join(t('common.listSeparator')) })
    emit('data-reset', props.character)
  } catch (err) {
    resetFeedback.value = err instanceof Error
      ? t('characterEdit.reset.failedWithReason', { reason: err.message })
      : t('characterEdit.reset.failed')
  } finally {
    resetBusy.value = null
  }
}

function handleClearMemories() {
  runReset(
    'memories',
    { memories: true },
    t('characterEdit.reset.confirmMemories', { name: props.character.name }),
  )
}

function handleClearConversations() {
  runReset(
    'conversations',
    { conversations: true },
    t('characterEdit.reset.confirmConversations', { name: props.character.name }),
  )
}

function handleClearAll() {
  runReset(
    'all',
    { memories: true, conversations: true, state_history: true },
    t('characterEdit.reset.confirmAll', { name: props.character.name }),
  )
}
</script>

<template>
  <div class="character-edit-panel">
    <div class="identity-warning">
      <strong>{{ t('characterEdit.identityWarningLabel') }}</strong>{{ t('characterEdit.identityWarning') }}
    </div>

    <div class="reset-panel">
      <div class="reset-panel-title">{{ t('characterEdit.reset.title') }}</div>
      <div class="reset-panel-hint">
        {{ t('characterEdit.reset.hint') }}
      </div>
      <div class="reset-panel-actions">
        <button
          type="button"
          class="btn-reset"
          :disabled="resetBusy !== null"
          @click="handleClearMemories"
        >{{ resetBusy === 'memories' ? t('characterEdit.reset.clearing') : t('characterEdit.reset.clearMemories') }}</button>
        <button
          type="button"
          class="btn-reset"
          :disabled="resetBusy !== null"
          @click="handleClearConversations"
        >{{ resetBusy === 'conversations' ? t('characterEdit.reset.clearing') : t('characterEdit.reset.clearConversations') }}</button>
        <button
          type="button"
          class="btn-reset btn-reset-danger"
          :disabled="resetBusy !== null"
          @click="handleClearAll"
        >{{ resetBusy === 'all' ? t('characterEdit.reset.clearing') : t('characterEdit.reset.clearAll') }}</button>
      </div>
      <div v-if="resetFeedback" class="reset-feedback">{{ resetFeedback }}</div>
    </div>

    <p v-if="shouldShowAdminLinks" class="admin-link-hint">
      {{ t('characterEdit.links.memories.prefix') }}
      <RouterLink
        :to="{ name: 'admin-memories', query: { character: character.id } }"
        class="admin-link-hint__link"
      >{{ t('characterEdit.links.memories.link') }}</RouterLink>
      {{ t('characterEdit.links.suffix') }}
    </p>

    <div class="form-section">
      <h3 class="section-title">{{ t('characterEdit.sections.basic') }}</h3>
      <label class="field-label">{{ t('characterCreate.fields.name.label') }}</label>
      <input v-model="form.name" class="field-input" :placeholder="t('characterCreate.fields.name.placeholder')" />

      <label class="field-label">{{ t('characterCreate.fields.summary.label') }}</label>
      <textarea v-model="form.summary" class="field-textarea" rows="2" :placeholder="t('characterCreate.fields.summary.placeholder')" />

      <label class="field-label">{{ t('characterCreate.fields.personality.label') }}</label>
      <input v-model="form.personality" class="field-input" :placeholder="t('characterCreate.fields.personality.placeholder')" />

      <label class="field-label">{{ t('characterCreate.fields.interests.label') }}</label>
      <input v-model="form.interests" class="field-input" :placeholder="t('characterCreate.fields.interests.placeholder')" />

      <label class="field-label">{{ t('characterCreate.fields.speakingStyle.label') }}</label>
      <input v-model="form.speaking_style" class="field-input" :placeholder="t('characterCreate.fields.speakingStyle.placeholder')" />

      <label class="field-label">{{ t('characterCreate.personalityType.label') }}</label>
      <select
        v-model="form.personality_type_code"
        class="field-select"
        @change="onPersonalityTypeChanged"
      >
        <option value="">{{ t('characterCreate.personalityType.unset') }}</option>
        <option
          v-for="code in PERSONALITY_TYPE_CODES"
          :key="code"
          :value="code"
        >
          {{ code }}
        </option>
      </select>
      <textarea
        v-if="form.personality_type_code"
        v-model="form.personality_type_rationale"
        class="field-textarea"
        rows="2"
        :placeholder="t('characterCreate.personalityType.rationalePlaceholder')"
      />
      <input
        v-if="form.personality_type_code"
        v-model="form.personality_type_notes"
        class="field-input"
        :placeholder="t('characterCreate.personalityType.notesPlaceholder')"
      />

      <label class="field-label">{{ t('characterCreate.fields.appearance.label') }}</label>
      <textarea
        v-model="form.appearance"
        class="field-textarea"
        rows="3"
        :placeholder="t('characterCreate.fields.appearance.placeholder')"
      />

      <CharacterIdentityFields
        v-model:gender-identity="form.gender_identity"
        v-model:third-person-pronoun="form.third_person_pronoun"
        v-model:visual-gender-presentation="form.visual_gender_presentation"
        v-model:visual-subject-type="form.visual_subject_type"
        v-model:visual-generation-style="form.visual_generation_style"
      />

      <label class="field-label">{{ t('characterCreate.fields.boundaries.label') }}</label>
      <input v-model="form.boundaries" class="field-input" :placeholder="t('characterCreate.fields.boundaries.placeholder')" />

      <label class="field-label">{{ t('characterCreate.fields.aspirations.label') }}</label>
      <input v-model="form.aspirations" class="field-input" :placeholder="t('characterCreate.fields.aspirations.placeholder')" />
      <div class="field-hint">{{ t('characterEdit.basic.aspirationsHint') }}</div>

      <label class="field-label">{{ t('characterEdit.basic.birthdayLabel') }}</label>
      <input v-model="form.date_of_birth" type="date" class="field-input" />
      <div class="field-hint">
        {{ t('characterEdit.basic.birthdayHint') }}
      </div>
    </div>

    <p v-if="shouldShowAdminLinks" class="admin-link-hint">
      {{ t('characterEdit.links.disposition.prefix') }}
      <RouterLink
        :to="{ name: 'admin-dispositions', query: { character: character.id } }"
        class="admin-link-hint__link"
      >{{ t('characterEdit.links.disposition.link') }}</RouterLink>
      {{ t('characterEdit.links.suffix') }}
    </p>

    <div class="form-section">
      <h3 class="section-title">{{ t('characterCreate.companions.title') }}</h3>
      <p class="field-hint">
        {{ t('characterEdit.companions.hint') }}
      </p>

      <div class="companion-gen-row">
        <input
          v-model="companionGenHint"
          class="field-input companion-gen-hint"
          :placeholder="t('characterEdit.companions.generatorPlaceholder')"
        />
        <button
          type="button"
          class="btn-companion-gen"
          :disabled="companionGenLoading || saving"
          @click="generateMoreCompanions"
        >
          {{ companionGenLoading ? t('characterCreate.draft.generating') : t('characterEdit.companions.generateAction') }}
        </button>
      </div>
      <div v-if="companionGenError" class="reset-feedback">
        {{ companionGenError }}
      </div>

      <div
        v-for="(npc, index) in companions"
        :key="index"
        class="companion-card"
      >
        <div class="companion-row companion-row--head">
          <input
            v-model="npc.name"
            class="field-input companion-name"
            :placeholder="t('characterCreate.companions.namePlaceholder')"
          />
          <input
            v-model="npc.role"
            class="field-input companion-role"
            :placeholder="t('characterCreate.companions.rolePlaceholder')"
          />
          <button
            type="button"
            class="btn-companion-remove"
            :disabled="saving"
            @click="removeCompanion(index)"
            :aria-label="t('characterCreate.companions.removeLabel')"
          >×</button>
        </div>
        <input
          v-model="npc.brief_profile"
          class="field-input"
          :placeholder="t('characterCreate.companions.profilePlaceholder')"
        />
        <input
          :value="companionSketchText(npc)"
          class="field-input"
          :placeholder="t('characterCreate.companions.sketchPlaceholder')"
          @input="
            updateCompanionSketch(
              index,
              ($event.target as HTMLInputElement).value,
            )
          "
        />
        <input
          v-model="npc.relationship_snippet"
          class="field-input"
          :placeholder="t('characterCreate.companions.relationshipPlaceholder')"
        />
      </div>

      <button
        type="button"
        class="btn-companion-add"
        :disabled="saving"
        @click="addCompanion"
      >{{ t('characterEdit.companions.addManualAction') }}</button>

      <div v-if="!companions.length" class="companions-empty">
        {{ t('characterEdit.companions.empty') }}
      </div>

      <p v-if="shouldShowTechnicalHints" class="field-hint">
        {{ t('characterEdit.companions.saveHint') }}
      </p>
    </div>

    <div v-if="shouldShowStateSettings" class="form-section">
      <h3 class="section-title">{{ t('characterEdit.sections.state') }}</h3>
      <label class="field-label">{{ t('characterEdit.state.emotion') }}</label>
      <input v-model="form.emotion" class="field-input" placeholder="neutral" />

      <div class="state-grid">
        <div class="state-item">
          <label class="field-label">{{ t('characterEdit.state.affection') }}</label>
          <input v-model.number="form.affection" type="range" min="0" max="100" class="field-range" />
          <span class="range-value">{{ form.affection }}</span>
        </div>
        <div class="state-item">
          <label class="field-label">{{ t('characterEdit.state.fatigue') }}</label>
          <input v-model.number="form.fatigue" type="range" min="0" max="100" class="field-range" />
          <span class="range-value">{{ form.fatigue }}</span>
        </div>
        <div class="state-item">
          <label class="field-label">{{ t('characterEdit.state.trust') }}</label>
          <input v-model.number="form.trust" type="range" min="0" max="100" class="field-range" />
          <span class="range-value">{{ form.trust }}</span>
        </div>
        <div class="state-item">
          <label class="field-label">{{ t('characterEdit.state.energy') }}</label>
          <input v-model.number="form.energy" type="range" min="0" max="100" class="field-range" />
          <span class="range-value">{{ form.energy }}</span>
        </div>
      </div>
    </div>

    <div v-if="showToolSettings !== false" class="form-section">
      <h3 class="section-title">{{ t('characterEdit.sections.tools') }}</h3>
      <p class="field-hint">
        {{ t('characterEdit.tools.hint') }}
      </p>
      <div v-if="visibleTools.length === 0" class="tools-empty">
        {{ t('characterEdit.tools.empty') }}
      </div>
      <div v-else class="tools-list">
        <label
          v-for="tool in visibleTools"
          :key="tool.name"
          class="tool-row"
        >
          <input
            type="checkbox"
            :checked="form.allowed_tools.includes(tool.name)"
            @change="toggleTool(tool.name, ($event.target as HTMLInputElement).checked)"
          />
          <div class="tool-info">
            <div class="tool-name">{{ tool.name }}</div>
            <div class="tool-desc">{{ toolDescription(tool) }}</div>
          </div>
        </label>
      </div>

      <!-- web_search is absent until a search provider is configured.
           Show a disabled placeholder so the capability isn't invisible. -->
      <div v-if="!hasWebSearchTool" class="tool-row tool-row--disabled">
        <input type="checkbox" disabled />
        <div class="tool-info">
          <div class="tool-name">web_search</div>
          <div class="tool-desc">{{ t('characterEdit.tools.webSearchUnconfigured.desc') }}</div>
          <p v-if="shouldShowAdminLinks" class="tool-empty-hint">
            {{ t('characterEdit.tools.webSearchUnconfigured.adminPrefix') }}
            <RouterLink
              :to="{ name: 'admin-providers' }"
              class="admin-link-hint__link"
            >{{ t('characterEdit.tools.webSearchUnconfigured.adminLink') }}</RouterLink>
            {{ t('characterEdit.tools.webSearchUnconfigured.adminSuffix') }}
          </p>
          <p v-else class="tool-empty-hint">
            {{ t('characterEdit.tools.webSearchUnconfigured.playerNote') }}
          </p>
        </div>
      </div>
    </div>

    <div v-if="shouldShowImageTriggerInfo" class="form-section">
      <h3 class="section-title">{{ t('characterEdit.sections.imageTrigger') }}</h3>
      <p class="field-hint">
        {{ t('characterEdit.imageTrigger.hintPrefix') }}
        <strong>{{ t('characterEdit.imageTrigger.hintStrong') }}</strong>
        {{ t('characterEdit.imageTrigger.hintSuffix') }}
      </p>
      <p class="field-hint field-hint-subtle">
        {{ t('characterEdit.imageTrigger.note') }}
      </p>
    </div>

    <p v-if="shouldShowAdminLinks" class="admin-link-hint">
      {{ t('characterEdit.links.proactive.prefix') }}
      <RouterLink
        :to="{ name: 'admin-proactive', query: { character: character.id } }"
        class="admin-link-hint__link"
      >{{ t('characterEdit.links.proactive.link') }}</RouterLink>
      {{ t('characterEdit.links.suffix') }}
    </p>

    <p v-if="shouldShowAdminLinks" class="admin-link-hint">
      {{ t('characterEdit.links.world.prefix') }}
      <RouterLink
        :to="{ name: 'admin-world', query: { character: character.id } }"
        class="admin-link-hint__link"
      >{{ t('characterEdit.links.world.link') }}</RouterLink>
      {{ t('characterEdit.links.suffix') }}
    </p>

    <CollapsibleSection :title="t('characterEdit.sections.relationships')" :default-open="false">
      <CharacterRelationshipsPanel
        :character="character"
        :characters="characters ?? []"
      />
    </CollapsibleSection>

    <p v-if="shouldShowAdminLinks" class="admin-link-hint">
      {{ t('characterEdit.links.voice.prefix') }}
      <RouterLink
        :to="{ name: 'admin-voice', query: { character: character.id } }"
        class="admin-link-hint__link"
      >{{ t('characterEdit.links.voice.link') }}</RouterLink>
      {{ t('characterEdit.links.suffix') }}
    </p>

    <div class="form-actions">
      <UiButton
        variant="primary"
        class="save-btn"
        :loading="saving"
        @click="handleSave"
      >{{ saving ? t('common.state.saving') : t('characterEdit.saveAction') }}</UiButton>
    </div>
  </div>
</template>

<style scoped>
.character-edit-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.identity-warning {
  background: rgba(255, 178, 102, 0.12);
  border: 1px solid rgba(255, 178, 102, 0.4);
  border-radius: 6px;
  padding: 10px 12px;
  font-size: var(--font-sm);
  line-height: 1.6;
  color: #ffd9a8;
}

.reset-panel {
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.reset-panel-title {
  font-size: var(--font-md);
  font-weight: 600;
}
.reset-panel-hint {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.reset-panel-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.btn-reset {
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--color-border);
  color: var(--color-text-primary);
  padding: 6px 12px;
  border-radius: 6px;
  font-size: var(--font-sm);
  cursor: pointer;
  transition: background 120ms ease;
}
.btn-reset:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.12);
}
.btn-reset:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-reset-danger {
  border-color: rgba(255, 120, 120, 0.5);
  color: #ff9999;
}
.btn-reset-danger:hover:not(:disabled) {
  background: rgba(255, 120, 120, 0.15);
}
.reset-feedback {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}

.admin-link-hint {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  padding: 8px 10px;
}
.admin-link-hint__link {
  color: var(--color-accent, #7eb6ff);
  text-decoration: none;
}
.admin-link-hint__link:hover {
  text-decoration: underline;
}

.form-section {
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.section-title {
  margin: 0 0 4px;
  font-size: var(--font-md);
  font-weight: 600;
}
.field-hint-subtle {
  opacity: 0.75;
}

.state-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px 16px;
}
.state-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.range-value {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  align-self: flex-end;
}

.tools-empty {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.tools-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.tool-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
}
.tool-row input[type="checkbox"] {
  margin-top: 3px;
  accent-color: var(--color-accent, #7eb6ff);
}
.tool-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.tool-name {
  font-size: var(--font-sm);
  font-weight: 600;
}
.tool-desc {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.tool-row--disabled {
  cursor: default;
  opacity: 0.6;
  border-style: dashed;
}
.tool-empty-hint {
  margin: 4px 0 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.companion-gen-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.companion-gen-hint {
  flex: 1;
  min-width: 0;
}
.btn-companion-gen {
  background: rgba(126, 182, 255, 0.18);
  border: 1px solid rgba(126, 182, 255, 0.4);
  color: #cbe4ff;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: var(--font-sm);
  cursor: pointer;
  white-space: nowrap;
}
.btn-companion-gen:hover:not(:disabled) {
  background: rgba(126, 182, 255, 0.28);
}
.btn-companion-gen:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.companion-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}
.companion-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.companion-row--head {
  align-items: stretch;
}
.companion-name {
  flex: 1;
}
.companion-role {
  flex: 1;
}
.btn-companion-remove {
  background: transparent;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  width: 28px;
  border-radius: 6px;
  font-size: 16px;
  cursor: pointer;
}
.btn-companion-remove:hover:not(:disabled) {
  color: #ff9999;
  border-color: rgba(255, 120, 120, 0.5);
}
.btn-companion-add {
  align-self: flex-start;
  background: rgba(255, 255, 255, 0.04);
  border: 1px dashed var(--color-border);
  color: var(--color-text-secondary);
  padding: 6px 12px;
  border-radius: 6px;
  font-size: var(--font-sm);
  cursor: pointer;
}
.btn-companion-add:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-primary);
}
.companions-empty {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  font-style: italic;
  padding: 4px 0;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
}
.save-btn {
  min-width: 120px;
}

code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: var(--font-xs);
}
</style>
