<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type {
  Character,
  CharacterDisposition,
  DispositionBand,
  OperatorPacePreference,
} from '@/types/character'
import { updateCharacter } from '@/utils/api/characters'
import { UiCard, UiButton } from '@/components/ui'

/**
 * Per-character editor for the four-dimensional CharacterDisposition VO.
 *
 * Remounted via ``:key="character.id"`` so setup() always snapshots a fresh
 * form from the incoming character — no manual watcher needed.
 *
 * LLM-first guardrail: all four dims are facts injected into the prompt
 * fact layer (chat + proactive). The hint text on every <select> reflects
 * that they are *tendencies* the LLM should embody, not behaviour
 * branches the backend code reads.
 */
type EditorSurface = 'admin' | 'player'

const props = withDefaults(defineProps<{
  character: Character
  patch: (updated: Character) => void
  surface?: EditorSurface
}>(), {
  surface: 'admin',
})

const { t } = useI18n()

interface DispositionForm {
  self_centeredness: DispositionBand
  candor: DispositionBand
  sharing_drive: DispositionBand
  associativeness: DispositionBand
  operator_pace_preference: OperatorPacePreference
}

interface DispositionField {
  key: keyof CharacterDisposition
  localeKey: 'selfCenteredness' | 'candor' | 'sharingDrive' | 'associativeness'
}

const DISPOSITION_FIELDS: DispositionField[] = [
  { key: 'self_centeredness', localeKey: 'selfCenteredness' },
  { key: 'candor', localeKey: 'candor' },
  { key: 'sharing_drive', localeKey: 'sharingDrive' },
  { key: 'associativeness', localeKey: 'associativeness' },
]
const BAND_ORDER: DispositionBand[] = ['low', 'medium', 'high']
const BAND_INDEX: Record<DispositionBand, number> = {
  low: 0,
  medium: 1,
  high: 2,
}

function snapshot(char: Character): DispositionForm {
  return {
    self_centeredness: char.disposition?.self_centeredness ?? 'medium',
    candor: char.disposition?.candor ?? 'medium',
    sharing_drive: char.disposition?.sharing_drive ?? 'medium',
    associativeness: char.disposition?.associativeness ?? 'medium',
    operator_pace_preference: char.operator_pace_preference ?? '',
  }
}

const form = ref<DispositionForm>(snapshot(props.character))
const saving = ref(false)
const errorMsg = ref<string | null>(null)
const successMsg = ref<string | null>(null)
const isPlayerSurface = computed(() => props.surface === 'player')
const copyRoot = computed(() => (
  isPlayerSurface.value ? 'playerAuthoring.dispositionEditor' : 'admin.dispositionEditor'
))

function copy(path: string, params?: Record<string, unknown>): string {
  return t(`${copyRoot.value}.${path}`, params ?? {})
}

function fieldCopy(field: DispositionField, path: string): string {
  return t(`${copyRoot.value}.fields.${field.localeKey}.${path}`)
}

function rangeValue(key: keyof CharacterDisposition): number {
  return BAND_INDEX[form.value[key]]
}

function setBandFromInput(key: keyof CharacterDisposition, event: Event) {
  const input = event.target as HTMLInputElement
  const index = Number(input.value)
  form.value[key] = BAND_ORDER[index] ?? 'medium'
}

async function handleSave() {
  saving.value = true
  errorMsg.value = null
  successMsg.value = null
  try {
    const disposition: CharacterDisposition = {
      self_centeredness: form.value.self_centeredness,
      candor: form.value.candor,
      sharing_drive: form.value.sharing_drive,
      associativeness: form.value.associativeness,
    }
    const updated = await updateCharacter(props.character.id, {
      disposition,
      operator_pace_preference: form.value.operator_pace_preference,
    })
    props.patch(updated)
    successMsg.value = copy('saved')
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : copy('saveFailed')
  } finally {
    saving.value = false
  }
}

watch(successMsg, (next) => {
  if (!next) return
  setTimeout(() => {
    if (successMsg.value === next) successMsg.value = null
  }, 2500)
})
</script>

<template>
  <div :class="['disposition-editor', { 'disposition-editor--player': isPlayerSurface }]">
    <UiCard v-if="!isPlayerSurface" size="lg">
      <template #header>
        <h2 class="disposition-editor__card-title">{{ copy('title', { name: character.name }) }}</h2>
      </template>

      <p class="disposition-editor__note">
        {{ copy('note') }}
      </p>

      <div
        v-for="field in DISPOSITION_FIELDS"
        :key="field.key"
        class="disposition-editor__field"
      >
        <label class="field-label">{{ fieldCopy(field, 'label') }}</label>
        <select v-model="form[field.key]" class="field-select">
          <option v-for="band in BAND_ORDER" :key="band" :value="band">
            {{ fieldCopy(field, `options.${band}`) }}
          </option>
        </select>
        <div v-if="field.localeKey === 'candor'" class="field-hint">
          {{ fieldCopy(field, 'hint') }}
        </div>
      </div>

      <div class="disposition-editor__field">
        <label class="field-label">{{ copy('fields.pacePreference.label') }}</label>
        <select v-model="form.operator_pace_preference" class="field-select">
          <option value="">{{ copy('fields.pacePreference.options.none') }}</option>
          <option value="more_active">{{ copy('fields.pacePreference.options.moreActive') }}</option>
          <option value="balanced">{{ copy('fields.pacePreference.options.balanced') }}</option>
          <option value="more_quiet">{{ copy('fields.pacePreference.options.moreQuiet') }}</option>
        </select>
        <div class="field-hint">
          {{ copy('fields.pacePreference.hint') }}
        </div>
      </div>
    </UiCard>

    <section v-else class="disposition-editor__player-panel">
      <h3 class="disposition-editor__player-title">
        {{ copy('title', { name: character.name }) }}
      </h3>
      <p class="disposition-editor__note">
        {{ copy('note') }}
      </p>

      <div
        v-for="field in DISPOSITION_FIELDS"
        :key="field.key"
        class="disposition-editor__field disposition-editor__range-field"
      >
        <div class="disposition-editor__range-heading">
          <label class="field-label">{{ fieldCopy(field, 'label') }}</label>
          <span class="disposition-editor__range-value">
            {{ fieldCopy(field, `options.${form[field.key]}`) }}
          </span>
        </div>
        <input
          type="range"
          min="0"
          max="2"
          step="1"
          class="field-range"
          :value="rangeValue(field.key)"
          :aria-label="fieldCopy(field, 'label')"
          @input="setBandFromInput(field.key, $event)"
        />
        <div class="disposition-editor__range-poles" aria-hidden="true">
          <span>{{ fieldCopy(field, 'lowPole') }}</span>
          <span>{{ fieldCopy(field, 'highPole') }}</span>
        </div>
        <div class="field-hint">{{ fieldCopy(field, 'hint') }}</div>
      </div>

      <div class="disposition-editor__field">
        <label class="field-label">{{ copy('fields.pacePreference.label') }}</label>
        <select v-model="form.operator_pace_preference" class="field-select">
          <option value="">{{ copy('fields.pacePreference.options.none') }}</option>
          <option value="more_active">{{ copy('fields.pacePreference.options.moreActive') }}</option>
          <option value="balanced">{{ copy('fields.pacePreference.options.balanced') }}</option>
          <option value="more_quiet">{{ copy('fields.pacePreference.options.moreQuiet') }}</option>
        </select>
        <div class="field-hint">
          {{ copy('fields.pacePreference.hint') }}
        </div>
      </div>
    </section>

    <div class="disposition-editor__actions">
      <div class="disposition-editor__status">
        <span v-if="errorMsg" class="disposition-editor__error">{{ errorMsg }}</span>
        <span v-else-if="successMsg" class="disposition-editor__success">{{ successMsg }}</span>
      </div>
      <UiButton
        variant="primary"
        :loading="saving"
        :disabled="saving"
        @click="handleSave"
      >{{ saving ? t('common.state.saving') : copy('saveAction') }}</UiButton>
    </div>
  </div>
</template>

<style scoped>
.disposition-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.disposition-editor--player {
  gap: var(--space-3);
}
.disposition-editor__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.disposition-editor__player-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.disposition-editor__player-title {
  margin: 0;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--color-primary-light);
}
.disposition-editor__note {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-3);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  line-height: 1.6;
}
.disposition-editor--player .disposition-editor__note {
  margin-bottom: 0;
}
.disposition-editor__field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-bottom: var(--space-3);
}
.disposition-editor--player .disposition-editor__field {
  margin-bottom: 0;
}
.disposition-editor__range-field {
  padding: var(--space-2) 0;
  border-bottom: 1px dashed var(--color-border);
}
.disposition-editor__range-heading,
.disposition-editor__range-poles {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}
.disposition-editor__range-value {
  color: var(--color-text);
  font-size: var(--font-xs);
  font-weight: 600;
  text-align: right;
}
.disposition-editor__range-poles {
  color: var(--color-text-secondary);
  font-size: 10px;
  line-height: 1.4;
}
.disposition-editor__actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
}
.disposition-editor__status {
  flex: 1;
  font-size: var(--font-sm);
}
.disposition-editor__error {
  color: #f4a3a3;
}
.disposition-editor__success {
  color: #6dd58c;
}
</style>
