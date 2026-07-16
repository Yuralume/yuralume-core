<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'

import type {
  Character,
  CharacterEncounter,
  CharacterRelationship,
} from '@/types/character'
import { formatDateTime } from '@/i18n/formatters'
import {
  createCharacterRelationship,
  listCharacterEncounters,
  listCharacterRelationships,
  tickCharacterEncounters,
  updateCharacterRelationship,
} from '@/utils/api/characters'
import { buildPeerProfileSeedPayload } from '@/utils/peerProfileSeed'

const props = defineProps<{
  character: Character
  characters: Character[]
}>()

const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()

const relationships = ref<CharacterRelationship[]>([])
const encounters = ref<CharacterEncounter[]>([])
const targetId = ref('')
const label = ref('')
const seedSummary = ref('')
const seedOccupation = ref('')
const seedHaunts = ref('')
const seedHabits = ref('')
const seedSharedActivities = ref('')
const seedRelationshipNote = ref('')
const loading = ref(false)
const saving = ref(false)
const ticking = ref(false)
const errorMsg = ref<string | null>(null)

const candidates = computed(() => {
  const linked = new Set(
    relationships.value.map((rel) => otherId(rel)).filter(Boolean),
  )
  return props.characters.filter((char) => {
    return char.id !== props.character.id && !linked.has(char.id)
  })
})

watch(
  () => props.character.id,
  () => reload(),
  { immediate: true },
)

async function reload() {
  loading.value = true
  errorMsg.value = null
  try {
    const [rels, encs] = await Promise.all([
      listCharacterRelationships(props.character.id),
      listCharacterEncounters(props.character.id),
    ])
    relationships.value = rels
    encounters.value = encs
    targetId.value = candidates.value[0]?.id ?? ''
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterRelationshipsPanel.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

async function addRelationship() {
  if (!targetId.value) return
  saving.value = true
  errorMsg.value = null
  try {
    const peerSeed = buildPeerProfileSeedPayload({
      summary: seedSummary.value,
      occupation: seedOccupation.value,
      haunts: seedHaunts.value,
      habits: seedHabits.value,
      relationshipNote: seedRelationshipNote.value,
      sharedActivities: seedSharedActivities.value,
    })
    await createCharacterRelationship(props.character.id, {
      target_character_id: targetId.value,
      relationship_label: label.value.trim(),
      ...(peerSeed ? { peer_profile_seed: peerSeed } : {}),
    })
    resetForm()
    await reload()
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterRelationshipsPanel.errors.createFailed')
  } finally {
    saving.value = false
  }
}

function resetForm() {
  label.value = ''
  seedSummary.value = ''
  seedOccupation.value = ''
  seedHaunts.value = ''
  seedHabits.value = ''
  seedSharedActivities.value = ''
  seedRelationshipNote.value = ''
}

async function toggleRelationship(rel: CharacterRelationship) {
  saving.value = true
  errorMsg.value = null
  try {
    const updated = await updateCharacterRelationship(rel.id, {
      enabled: !rel.enabled,
    })
    relationships.value = relationships.value.map((item) => {
      return item.id === updated.id ? updated : item
    })
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterRelationshipsPanel.errors.updateFailed')
  } finally {
    saving.value = false
  }
}

async function tick() {
  ticking.value = true
  errorMsg.value = null
  try {
    await tickCharacterEncounters()
    await reload()
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterRelationshipsPanel.errors.tickFailed')
  } finally {
    ticking.value = false
  }
}

function otherId(rel: CharacterRelationship): string {
  return rel.character_a_id === props.character.id
    ? rel.character_b_id
    : rel.character_a_id
}

function otherName(rel: CharacterRelationship): string {
  const id = otherId(rel)
  return props.characters.find((char) => char.id === id)?.name ?? id
}

function encounterSummary(encounter: CharacterEncounter): string {
  if (encounter.character_a_id === props.character.id) {
    return encounter.summary_for_a || encounter.trigger_reason
  }
  return encounter.summary_for_b || encounter.trigger_reason
}

function formatEncounterTime(iso: string | null): string {
  if (!iso) return t('common.fallback.none')
  return formatDateTime(iso, locale.value, timeZone.value)
}

function encounterStatusLabel(status: string): string {
  const key = {
    planned: 'characterRelationshipsPanel.status.planned',
    running: 'characterRelationshipsPanel.status.running',
    completed: 'characterRelationshipsPanel.status.completed',
    failed: 'characterRelationshipsPanel.status.failed',
  }[status]
  return key ? t(key) : status
}

function extractError(err: unknown): string | null {
  if (err instanceof Error) return err.message
  return null
}
</script>

<template>
  <div class="relationships-panel">
    <div class="relationship-toolbar">
      <select v-model="targetId" class="field-select" :disabled="!candidates.length || saving">
        <option value="" disabled>{{ t('characterRelationshipsPanel.selectCharacter') }}</option>
        <option v-for="char in candidates" :key="char.id" :value="char.id">
          {{ char.name }}
        </option>
      </select>
      <input
        v-model="label"
        class="field-input"
        type="text"
        :placeholder="t('characterRelationshipsPanel.labelPlaceholder')"
        :disabled="saving"
      />
      <button class="chip-btn primary" :disabled="!targetId || saving" @click="addRelationship">
        {{ saving ? t('common.state.saving') : t('characterRelationshipsPanel.add') }}
      </button>
    </div>
    <div class="peer-seed-grid">
      <label>
        <span class="field-label">{{ t('characterRelationshipsPanel.seed.summary') }}</span>
        <textarea
          v-model="seedSummary"
          class="field-textarea"
          :placeholder="t('characterRelationshipsPanel.seed.summaryPlaceholder')"
          :disabled="saving"
        ></textarea>
      </label>
      <label>
        <span class="field-label">{{ t('characterRelationshipsPanel.seed.occupation') }}</span>
        <input
          v-model="seedOccupation"
          class="field-input"
          type="text"
          :placeholder="t('characterRelationshipsPanel.seed.occupationPlaceholder')"
          :disabled="saving"
        />
      </label>
      <label>
        <span class="field-label">{{ t('characterRelationshipsPanel.seed.haunts') }}</span>
        <input
          v-model="seedHaunts"
          class="field-input"
          type="text"
          :placeholder="t('characterRelationshipsPanel.seed.hauntsPlaceholder')"
          :disabled="saving"
        />
      </label>
      <label>
        <span class="field-label">{{ t('characterRelationshipsPanel.seed.habits') }}</span>
        <input
          v-model="seedHabits"
          class="field-input"
          type="text"
          :placeholder="t('characterRelationshipsPanel.seed.habitsPlaceholder')"
          :disabled="saving"
        />
      </label>
      <label>
        <span class="field-label">{{ t('characterRelationshipsPanel.seed.sharedActivities') }}</span>
        <input
          v-model="seedSharedActivities"
          class="field-input"
          type="text"
          :placeholder="t('characterRelationshipsPanel.seed.sharedActivitiesPlaceholder')"
          :disabled="saving"
        />
      </label>
      <label>
        <span class="field-label">{{ t('characterRelationshipsPanel.seed.relationshipNote') }}</span>
        <textarea
          v-model="seedRelationshipNote"
          class="field-textarea"
          :placeholder="t('characterRelationshipsPanel.seed.relationshipNotePlaceholder')"
          :disabled="saving"
        ></textarea>
      </label>
    </div>

    <div v-if="loading" class="empty-hint">{{ t('common.state.loading') }}</div>

    <div v-else-if="relationships.length === 0" class="empty-hint">
      {{ t('characterRelationshipsPanel.emptyRelationships') }}
    </div>

    <ul v-else class="relationship-list">
      <li v-for="rel in relationships" :key="rel.id" class="relationship-row">
        <div class="relationship-main">
          <span class="name">{{ otherName(rel) }}</span>
          <span class="label">{{ rel.relationship_label || t('characterRelationshipsPanel.unlabeled') }}</span>
          <span :class="['status', { off: !rel.enabled }]">
            {{ rel.enabled ? t('characterRelationshipsPanel.enabled') : t('characterRelationshipsPanel.disabled') }}
          </span>
        </div>
        <div class="relationship-meta">
          {{ t('characterRelationshipsPanel.meta.affection', { a: rel.affection_a_to_b, b: rel.affection_b_to_a }) }} ·
          {{ t('characterRelationshipsPanel.meta.trust', { a: rel.trust_a_to_b, b: rel.trust_b_to_a }) }} ·
          {{ t('characterRelationshipsPanel.meta.recent', { time: formatEncounterTime(rel.last_interaction_at) }) }}
        </div>
        <button class="chip-btn" :disabled="saving" @click="toggleRelationship(rel)">
          {{ rel.enabled ? t('characterRelationshipsPanel.disable') : t('characterRelationshipsPanel.enable') }}
        </button>
      </li>
    </ul>

    <div class="encounter-head">
      <span>{{ t('characterRelationshipsPanel.encountersTitle') }}</span>
      <button class="chip-btn" :disabled="ticking" @click="tick">
        {{ ticking ? t('characterRelationshipsPanel.ticking') : t('characterRelationshipsPanel.manualTick') }}
      </button>
    </div>

    <ul v-if="encounters.length" class="encounter-list">
      <li v-for="encounter in encounters.slice(0, 5)" :key="encounter.id" class="encounter-row">
        <div class="encounter-top">
          <span>{{ formatEncounterTime(encounter.scheduled_for) }}</span>
          <span class="status">{{ encounterStatusLabel(encounter.status) }}</span>
        </div>
        <div class="encounter-summary">{{ encounterSummary(encounter) }}</div>
      </li>
    </ul>
    <div v-else class="empty-hint compact">{{ t('characterRelationshipsPanel.emptyEncounters') }}</div>

    <div v-if="errorMsg" class="error-msg">{{ errorMsg }}</div>
  </div>
</template>

<style scoped>
.relationships-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.relationship-toolbar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
  gap: 6px;
}

.peer-seed-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
}

.peer-seed-grid label {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 4px;
}

.peer-seed-grid textarea {
  min-height: 72px;
}

.chip-btn {
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  border-radius: 999px;
  cursor: pointer;
  white-space: nowrap;
}

.chip-btn.primary {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}

.chip-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.relationship-list,
.encounter-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  list-style: none;
  padding: 0;
  margin: 0;
}

.relationship-row,
.encounter-row {
  padding: 8px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
}

.relationship-main,
.encounter-top,
.encounter-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.encounter-head {
  justify-content: space-between;
  color: var(--color-text);
  font-size: 12px;
  font-weight: 700;
}

.name {
  color: var(--color-text);
  font-weight: 700;
}

.label,
.relationship-meta,
.encounter-summary {
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.5;
}

.status {
  margin-left: auto;
  color: var(--color-primary-light);
  font-size: 11px;
}

.status.off {
  color: var(--color-text-secondary);
}

.empty-hint {
  padding: 12px;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  color: var(--color-text-secondary);
  font-size: 12px;
  text-align: center;
}

.empty-hint.compact {
  padding: 8px;
}

.error-msg {
  padding: 6px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}

@media (max-width: 640px) {
  .relationship-toolbar {
    grid-template-columns: 1fr;
  }

  .peer-seed-grid {
    grid-template-columns: 1fr;
  }
}
</style>
