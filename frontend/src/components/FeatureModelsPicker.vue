<script setup lang="ts">
import { computed, ref, watch, onMounted } from 'vue'
import axios from 'axios'
import { useI18n } from 'vue-i18n'
import {
  getActiveModelPreference,
  setActiveModelPreference,
  getFeatureModelPreferences,
  setFeatureModelPreferences,
  getFeatureModelGroups,
  updateFeatureModelGroups,
  getCharacterFeatureModelPreferences,
  setCharacterFeatureModelPreferences,
  listProviderModels,
  hasReasoningOverride,
  type FeatureModelEntry,
  type FeatureModelGroupSummary,
  type FeatureReasoningOverride,
} from '@/utils/api/system'
import { notification } from 'ant-design-vue'
import { UiButton, UiCombobox } from '@/components/ui'
import ReasoningOverrideFields from '@/components/ReasoningOverrideFields.vue'
import VisionOverrideSelect from '@/components/VisionOverrideSelect.vue'
import { featureKeyLabel, type FeatureChip } from '@/utils/catalogLabels'

/** Picker for both the global per-feature preference and per-character
 * overrides. ``characterId === undefined`` → global mode (writes the
 * primary ``active_model`` plus shared ``feature_models`` pref);
 * ``characterId`` set → character mode (writes to the character row's
 * ``feature_models`` field, includes the ``chat`` feature key which the
 * global mode omits). */
const props = defineProps<{
  /** List of provider IDs the registry knows about. We render one
   * provider <select> per active/feature row. */
  providers: string[]
  /** Optional character id. When set, the picker reads/writes the
   * per-character endpoint instead of the global preferences. */
  characterId?: string
}>()

const { t, te } = useI18n()

/** Known feature keys + labels come from the backend so we don't have
 * to keep a parallel list in TS. */
const knownKeys = ref<string[]>([])
const labels = ref<Record<string, string>>({})
const groups = ref<FeatureModelGroupSummary[]>([])
const groupOverrides = ref<Record<string, FeatureModelEntry>>({})

/** Working copy of the overrides. Keyed by feature_key; a blank
 * provider_id means "inherit from the next layer up" (per-character →
 * global feature_models → global active_model → container default). */
const overrides = ref<Record<string, FeatureModelEntry>>({})

/** Global mode only — the main provider/model used when no feature or
 * character override is set. */
const activeProviderId = ref<string | null>(null)
const activeModelId = ref<string | null>(null)
/** Tri-state vision override on the active/default model — null inherits
 * the connection flag, true/false pin it. */
const activeSupportsVision = ref<boolean | null>(null)

/** Model lists cached per provider so each row's model dropdown can
 * populate without spamming /models on every keystroke. */
const modelsByProvider = ref<Record<string, string[]>>({})
const loadingModels = ref<Record<string, boolean>>({})

const saving = ref(false)
const loaded = ref(false)

function saveErrorDescription(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string' && detail.trim()) return detail
  }
  return error instanceof Error ? error.message : String(error)
}

const featureToGroup = computed(() => {
  const mapping: Record<string, FeatureModelGroupSummary> = {}
  for (const group of groups.value) {
    for (const member of group.members) {
      mapping[member.key] = group
    }
  }
  return mapping
})

async function loadPreferences() {
  loaded.value = false
  try {
    let activePref: Awaited<ReturnType<typeof getActiveModelPreference>> | null = null
    let prefs:
      | Awaited<ReturnType<typeof getFeatureModelPreferences>>
      | Awaited<ReturnType<typeof getCharacterFeatureModelPreferences>>
    if (props.characterId) {
      groups.value = []
      groupOverrides.value = {}
      prefs = await getCharacterFeatureModelPreferences(props.characterId)
    } else {
      const [activeResult, featureResult, groupResult] = await Promise.all([
        getActiveModelPreference(),
        getFeatureModelPreferences(),
        getFeatureModelGroups(),
      ])
      activePref = activeResult
      prefs = featureResult
      groups.value = groupResult.groups
      groupOverrides.value = Object.fromEntries(
        groupResult.groups.map((group) => [
          group.key,
          group.model
            ? {
                provider_id: group.model.provider_id,
                model_id: group.model.model_id,
                reasoning: group.model.reasoning ?? null,
                supports_vision: group.model.supports_vision ?? null,
              }
            : {
                provider_id: null,
                model_id: null,
                reasoning: null,
                supports_vision: null,
              },
        ]),
      )
    }
    activeProviderId.value = activePref?.provider_id ?? null
    activeModelId.value = activePref?.model_id ?? null
    activeSupportsVision.value = activePref?.supports_vision ?? null
    knownKeys.value = prefs.known_keys
    labels.value = prefs.labels
    // Ensure every known key has an entry (blank) so the UI shows a
    // picker row per feature. Strip the ``feature_key`` field from
    // per-character entries since the picker only stores provider/model.
    // Character entries carry no reasoning (global routing concern).
    const merged: Record<string, FeatureModelEntry> = {}
    for (const key of prefs.known_keys) {
      const raw = prefs.overrides[key]
      merged[key] = raw
        ? {
            provider_id: raw.provider_id,
            model_id: raw.model_id,
            reasoning: 'reasoning' in raw ? (raw.reasoning ?? null) : null,
            supports_vision:
              'supports_vision' in raw ? (raw.supports_vision ?? null) : null,
          }
        : {
            provider_id: null,
            model_id: null,
            reasoning: null,
            supports_vision: null,
          }
    }
    overrides.value = merged
    loaded.value = true
    // Pre-fetch model lists for providers already pinned so the model
    // dropdown is populated on open without a per-click fetch.
    if (activeProviderId.value) {
      void ensureModelsFor(activeProviderId.value)
    }
    for (const entry of Object.values(merged)) {
      if (entry.provider_id) {
        void ensureModelsFor(entry.provider_id)
      }
    }
    for (const entry of Object.values(groupOverrides.value)) {
      if (entry.provider_id) {
        void ensureModelsFor(entry.provider_id)
      }
    }
  } catch (error) {
    notification.error({
      message: props.characterId
        ? t('featureModelsPicker.errors.loadCharacterFailed')
        : t('featureModelsPicker.errors.loadGlobalFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  }
}

async function ensureModelsFor(providerId: string) {
  if (!providerId) return
  if (modelsByProvider.value[providerId]) return
  if (loadingModels.value[providerId]) return
  loadingModels.value[providerId] = true
  try {
    const list = await listProviderModels(providerId)
    modelsByProvider.value[providerId] = list
  } catch {
    modelsByProvider.value[providerId] = []
  } finally {
    loadingModels.value[providerId] = false
  }
}

function onActiveProviderChange(providerId: string) {
  activeProviderId.value = providerId || null
  activeModelId.value = null
  if (providerId) void ensureModelsFor(providerId)
}

function onActiveModelChange(modelId: string) {
  activeModelId.value = modelId || null
}

function onActiveVisionChange(value: boolean | null) {
  activeSupportsVision.value = value
}

function onProviderChange(key: string, providerId: string) {
  // Picking a new provider invalidates the prior model_id — force
  // the model dropdown to "use provider default" (null) until the
  // operator picks something valid for the new provider. The
  // reasoning override is a separate dimension and survives the swap.
  overrides.value[key] = {
    provider_id: providerId || null,
    model_id: null,
    reasoning: overrides.value[key]?.reasoning ?? null,
  }
  if (providerId) void ensureModelsFor(providerId)
}

function onModelChange(key: string, modelId: string) {
  const current = overrides.value[key] ?? { provider_id: null, model_id: null }
  overrides.value[key] = {
    ...current,
    model_id: modelId || null,
  }
}

function onReasoningChange(
  key: string,
  reasoning: FeatureReasoningOverride | null,
) {
  const current = overrides.value[key] ?? { provider_id: null, model_id: null }
  overrides.value[key] = { ...current, reasoning }
}

function onVisionChange(key: string, value: boolean | null) {
  const current = overrides.value[key] ?? { provider_id: null, model_id: null }
  overrides.value[key] = { ...current, supports_vision: value }
}

function onGroupProviderChange(groupKey: string, providerId: string) {
  groupOverrides.value[groupKey] = {
    provider_id: providerId || null,
    model_id: null,
    reasoning: groupOverrides.value[groupKey]?.reasoning ?? null,
  }
  if (providerId) void ensureModelsFor(providerId)
}

function onGroupModelChange(groupKey: string, modelId: string) {
  const current = groupOverrides.value[groupKey] ?? {
    provider_id: null,
    model_id: null,
  }
  groupOverrides.value[groupKey] = {
    ...current,
    model_id: modelId || null,
  }
}

function onGroupReasoningChange(
  groupKey: string,
  reasoning: FeatureReasoningOverride | null,
) {
  const current = groupOverrides.value[groupKey] ?? {
    provider_id: null,
    model_id: null,
  }
  groupOverrides.value[groupKey] = { ...current, reasoning }
}

function onGroupVisionChange(groupKey: string, value: boolean | null) {
  const current = groupOverrides.value[groupKey] ?? {
    provider_id: null,
    model_id: null,
  }
  groupOverrides.value[groupKey] = { ...current, supports_vision: value }
}

function clearRow(key: string) {
  overrides.value[key] = {
    provider_id: null,
    model_id: null,
    reasoning: null,
    supports_vision: null,
  }
}

function clearGroup(groupKey: string) {
  groupOverrides.value[groupKey] = {
    provider_id: null,
    model_id: null,
    reasoning: null,
    supports_vision: null,
  }
}

function rowHasOverride(entry?: FeatureModelEntry) {
  return Boolean(entry?.provider_id || entry?.model_id)
}

/** Whether a tri-state vision pin is set (true OR false — both are
 * explicit assertions; only null/undefined inherit the connection). */
function hasVisionOverride(entry?: FeatureModelEntry) {
  return typeof entry?.supports_vision === 'boolean'
}

/** Whether the entry pins anything at all — model pin OR reasoning
 * posture OR vision pin. Drives the clear button and the save-payload
 * filter; ``rowHasOverride`` stays model-only for the effective-source
 * label. */
function rowHasAnySetting(entry?: FeatureModelEntry) {
  return (
    rowHasOverride(entry) ||
    hasReasoningOverride(entry?.reasoning) ||
    hasVisionOverride(entry)
  )
}

function groupLabelForFeature(featureKey: string) {
  const group = featureToGroup.value[featureKey]
  return group ? groupText(group, 'label') : ''
}

/** Localized label for a feature key. The backend still ships the
 * Chinese ``FEATURE_LABELS`` value (as ``member.label`` in groups, or
 * ``labels[key]`` for the advanced rows); route it through the
 * ``featureKeys`` namespace so non-zh UIs stop leaking Chinese, falling
 * back to the backend string for any not-yet-translated key. */
function featureLabel(featureKey: string, fallback?: string) {
  const chip: FeatureChip = {
    key: featureKey,
    label: fallback ?? labels.value[featureKey] ?? featureKey,
  }
  return featureKeyLabel(t, chip)
}

function groupText(
  group: FeatureModelGroupSummary,
  field: 'label' | 'description' | 'modelGuidance',
) {
  const key = `featureModelsPicker.groups.${group.key}.${field}`
  if (te(key)) {
    return t(key)
  }
  if (field === 'modelGuidance') {
    return group.model_guidance
  }
  return group[field]
}

function effectiveSourceLabel(featureKey: string) {
  if (rowHasOverride(overrides.value[featureKey])) {
    return t('featureModelsPicker.source.feature')
  }
  const group = featureToGroup.value[featureKey]
  if (group && rowHasOverride(groupOverrides.value[group.key])) {
    return t('featureModelsPicker.source.group')
  }
  if (activeProviderId.value) {
    return t('featureModelsPicker.source.active')
  }
  return t('featureModelsPicker.source.runtime')
}

async function handleSave() {
  saving.value = true
  try {
    if (props.characterId) {
      // Character endpoint expects entries shaped like
      // ``{feature_key, provider_id, model_id}`` per row.
      const payloadOverrides: Record<string, {
        feature_key: string; provider_id: string | null; model_id: string | null
      }> = {}
      for (const [key, entry] of Object.entries(overrides.value)) {
        if (!entry.provider_id && !entry.model_id) continue
        payloadOverrides[key] = {
          feature_key: key,
          provider_id: entry.provider_id,
          model_id: entry.model_id,
        }
      }
      const result = await setCharacterFeatureModelPreferences(
        props.characterId, payloadOverrides,
      )
      const merged: Record<string, FeatureModelEntry> = {}
      for (const key of knownKeys.value) {
        const raw = result.overrides[key]
        merged[key] = raw
          ? { provider_id: raw.provider_id, model_id: raw.model_id }
          : { provider_id: null, model_id: null }
      }
      overrides.value = merged
      notification.success({
        message: t('featureModelsPicker.savedCharacter'),
        duration: 2,
      })
    } else {
      const activeResult = await setActiveModelPreference({
        provider_id: activeProviderId.value,
        model_id: activeProviderId.value ? activeModelId.value : null,
        supports_vision: activeSupportsVision.value,
      })
      activeProviderId.value = activeResult.provider_id
      activeModelId.value = activeResult.model_id
      activeSupportsVision.value = activeResult.supports_vision ?? null

      const payloadGroups: Record<string, FeatureModelEntry> = {}
      for (const [key, entry] of Object.entries(groupOverrides.value)) {
        if (!rowHasAnySetting(entry)) continue
        payloadGroups[key] = {
          provider_id: entry.provider_id,
          model_id: entry.model_id,
          reasoning: hasReasoningOverride(entry.reasoning)
            ? entry.reasoning
            : null,
          supports_vision: hasVisionOverride(entry)
            ? entry.supports_vision
            : null,
        }
      }
      const groupResult = await updateFeatureModelGroups({
        feature_model_groups: payloadGroups,
      })
      groups.value = groupResult.groups
      groupOverrides.value = Object.fromEntries(
        groupResult.groups.map((group) => [
          group.key,
          group.model
            ? {
                provider_id: group.model.provider_id,
                model_id: group.model.model_id,
                reasoning: group.model.reasoning ?? null,
                supports_vision: group.model.supports_vision ?? null,
              }
            : {
                provider_id: null,
                model_id: null,
                reasoning: null,
                supports_vision: null,
              },
        ]),
      )

      // Global endpoint — provider/model plus optional reasoning.
      const payloadOverrides: Record<string, FeatureModelEntry> = {}
      for (const [key, entry] of Object.entries(overrides.value)) {
        if (!rowHasAnySetting(entry)) continue
        payloadOverrides[key] = {
          provider_id: entry.provider_id,
          model_id: entry.model_id,
          reasoning: hasReasoningOverride(entry.reasoning)
            ? entry.reasoning
            : null,
          supports_vision: hasVisionOverride(entry)
            ? entry.supports_vision
            : null,
        }
      }
      const result = await setFeatureModelPreferences({
        overrides: payloadOverrides,
        known_keys: knownKeys.value,
        labels: labels.value,
      })
      const merged: Record<string, FeatureModelEntry> = {}
      for (const key of knownKeys.value) {
        const raw = result.overrides[key]
        merged[key] = raw
          ? {
              provider_id: raw.provider_id,
              model_id: raw.model_id,
              reasoning: raw.reasoning ?? null,
              supports_vision: raw.supports_vision ?? null,
            }
          : {
              provider_id: null,
              model_id: null,
              reasoning: null,
              supports_vision: null,
            }
      }
      overrides.value = merged
      notification.success({
        message: t('featureModelsPicker.savedGlobal'),
        duration: 2,
      })
    }
  } catch (error) {
    notification.error({
      message: t('featureModelsPicker.errors.saveFailed'),
      description: saveErrorDescription(error),
      duration: 4,
    })
  } finally {
    saving.value = false
  }
}

onMounted(loadPreferences)

// Switching characters in the same panel needs to reload the overrides;
// global mode never changes so this is a no-op there.
watch(() => props.characterId, () => {
  void loadPreferences()
})

// If the parent's provider list changes (rare — only on container
// reboot), refresh model caches lazily.
watch(() => props.providers, () => {
  modelsByProvider.value = {}
})
</script>

<template>
  <div class="feature-models-picker">
    <p class="hint">
      <template v-if="characterId">
        {{ t('featureModelsPicker.characterHintPrefix') }}
        <strong>{{ t('featureModelsPicker.blank') }}</strong>{{ t('featureModelsPicker.characterHintSuffix') }}
      </template>
      <template v-else>
        {{ t('featureModelsPicker.globalHintPrefix') }}
        <strong>{{ t('featureModelsPicker.blank') }}</strong>{{ t('featureModelsPicker.globalHintMiddle') }}
        <strong>{{ t('featureModelsPicker.providerOnly') }}</strong>{{ t('featureModelsPicker.globalHintSuffix') }}
      </template>
    </p>

    <div v-if="!loaded" class="loading-hint">{{ t('common.state.loading') }}</div>

    <div v-else class="feature-rows">
      <div v-if="!characterId" class="active-row">
        <div class="feature-label">{{ t('featureModelsPicker.activeLabel') }}</div>
        <div class="feature-selects">
          <select
            :value="activeProviderId ?? ''"
            class="field-select"
            @change="onActiveProviderChange(($event.target as HTMLSelectElement).value)"
          >
            <option value="">{{ t('featureModelsPicker.useRuntimeDefault') }}</option>
            <option v-for="p in providers" :key="p" :value="p">{{ p }}</option>
          </select>
          <UiCombobox
            :model-value="activeModelId ?? ''"
            :options="modelsByProvider[activeProviderId ?? ''] ?? []"
            :disabled="!activeProviderId"
            :loading="loadingModels[activeProviderId ?? ''] ?? false"
            :placeholder="t('featureModelsPicker.providerDefault')"
            :aria-label="t('featureModelsPicker.activeLabel')"
            @update:model-value="onActiveModelChange"
          />
        </div>
        <VisionOverrideSelect
          :model-value="activeSupportsVision"
          @update:model-value="onActiveVisionChange"
        />
      </div>

      <section v-if="!characterId" class="group-section">
        <div class="section-heading">
          <h3>{{ t('featureModelsPicker.groupsTitle') }}</h3>
          <span>{{ t('featureModelsPicker.groupsHint') }}</span>
        </div>
        <div
          v-for="group in groups"
          :key="group.key"
          class="group-row"
        >
          <div class="group-row__main">
            <div>
              <div class="feature-label feature-label--strong">
                {{ groupText(group, 'label') }}
              </div>
              <p class="group-description">{{ groupText(group, 'description') }}</p>
              <p class="group-guidance">
                <span>{{ t('featureModelsPicker.modelGuidanceLabel') }}</span>
                {{ groupText(group, 'modelGuidance') }}
              </p>
              <details class="members-detail">
                <summary>
                  {{ t('featureModelsPicker.memberCount', { count: group.members.length }) }}
                </summary>
                <div class="member-list">
                  <span
                    v-for="member in group.members"
                    :key="member.key"
                    class="member-chip"
                  >{{ featureLabel(member.key, member.label) }}</span>
                </div>
              </details>
            </div>
            <div class="feature-controls">
              <div class="feature-selects">
                <select
                  :value="groupOverrides[group.key]?.provider_id ?? ''"
                  class="field-select"
                  @change="onGroupProviderChange(group.key, ($event.target as HTMLSelectElement).value)"
                >
                  <option value="">{{ t('featureModelsPicker.inheritActive') }}</option>
                  <option v-for="p in providers" :key="p" :value="p">{{ p }}</option>
                </select>
                <UiCombobox
                  :model-value="groupOverrides[group.key]?.model_id ?? ''"
                  :options="modelsByProvider[groupOverrides[group.key]?.provider_id ?? ''] ?? []"
                  :disabled="!groupOverrides[group.key]?.provider_id"
                  :loading="loadingModels[groupOverrides[group.key]?.provider_id ?? ''] ?? false"
                  :placeholder="t('featureModelsPicker.providerDefault')"
                  @update:model-value="onGroupModelChange(group.key, $event)"
                />
                <button
                  v-if="rowHasAnySetting(groupOverrides[group.key])"
                  type="button"
                  class="btn-clear"
                  :title="t('featureModelsPicker.clearGroupTitle')"
                  @click="clearGroup(group.key)"
                >×</button>
              </div>
              <ReasoningOverrideFields
                :model-value="groupOverrides[group.key]?.reasoning ?? null"
                @update:model-value="onGroupReasoningChange(group.key, $event)"
              />
              <VisionOverrideSelect
                :model-value="groupOverrides[group.key]?.supports_vision ?? null"
                @update:model-value="onGroupVisionChange(group.key, $event)"
              />
            </div>
          </div>
        </div>
      </section>

      <details class="advanced-section" :open="Boolean(characterId)">
        <summary class="advanced-summary">
          {{
            characterId
              ? t('featureModelsPicker.characterOverridesTitle')
              : t('featureModelsPicker.advancedTitle')
          }}
        </summary>
        <div
          v-for="key in knownKeys"
          :key="key"
          class="feature-row"
        >
          <div>
            <div class="feature-label">{{ featureLabel(key) }}</div>
            <div v-if="!characterId" class="feature-meta">
              <span>{{ t('featureModelsPicker.groupLabel') }}: {{ groupLabelForFeature(key) }}</span>
              <span>{{ t('featureModelsPicker.sourceLabel') }}: {{ effectiveSourceLabel(key) }}</span>
            </div>
          </div>
          <div class="feature-controls">
            <div class="feature-selects">
              <select
                :value="overrides[key]?.provider_id ?? ''"
                class="field-select"
                @change="onProviderChange(key, ($event.target as HTMLSelectElement).value)"
              >
                <option value="">{{ characterId ? t('featureModelsPicker.inheritPrimary') : t('featureModelsPicker.inheritGroup') }}</option>
                <option v-for="p in providers" :key="p" :value="p">{{ p }}</option>
              </select>
              <UiCombobox
                :model-value="overrides[key]?.model_id ?? ''"
                :options="modelsByProvider[overrides[key]?.provider_id ?? ''] ?? []"
                :disabled="!overrides[key]?.provider_id"
                :loading="loadingModels[overrides[key]?.provider_id ?? ''] ?? false"
                :placeholder="t('featureModelsPicker.providerDefault')"
                @update:model-value="onModelChange(key, $event)"
              />
              <button
                v-if="rowHasAnySetting(overrides[key])"
                type="button"
                class="btn-clear"
                :title="t('featureModelsPicker.clearRowTitle')"
                @click="clearRow(key)"
              >×</button>
            </div>
            <ReasoningOverrideFields
              v-if="!characterId"
              :model-value="overrides[key]?.reasoning ?? null"
              @update:model-value="onReasoningChange(key, $event)"
            />
            <VisionOverrideSelect
              v-if="!characterId"
              :model-value="overrides[key]?.supports_vision ?? null"
              @update:model-value="onVisionChange(key, $event)"
            />
          </div>
        </div>
      </details>
    </div>

    <div class="actions">
      <UiButton
        variant="primary"
        :loading="saving"
        :disabled="!loaded"
        @click="handleSave"
      >{{
        saving
          ? t('common.state.saving')
          : (characterId ? t('featureModelsPicker.saveCharacter') : t('featureModelsPicker.saveGlobal'))
      }}</UiButton>
    </div>
  </div>
</template>

<style scoped>
.feature-models-picker {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin: 0;
}

.loading-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  padding: 12px 0;
}

.active-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--color-border);
}

.feature-rows {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.feature-row,
.group-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.group-section,
.advanced-section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.section-heading h3 {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-primary);
}

.section-heading span,
.feature-meta,
.group-description,
.group-guidance,
.members-detail {
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.45;
}

.group-row,
.advanced-section {
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.02);
}

.group-row__main {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(320px, 0.9fr);
  gap: 10px;
  align-items: start;
}

.group-description {
  margin: 2px 0 0;
}

.group-guidance {
  margin: 4px 0 0;
}

.group-guidance span {
  color: var(--color-text-primary);
  font-weight: 600;
}

.members-detail {
  margin-top: 6px;
}

.members-detail summary {
  cursor: pointer;
}

.member-list {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.member-chip {
  border: 1px solid var(--color-border);
  border-radius: 999px;
  padding: 2px 7px;
  color: var(--color-text-secondary);
}

.advanced-summary {
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.advanced-section[open] {
  gap: 10px;
}

.feature-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 2px;
}

.feature-label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.feature-label--strong {
  font-weight: 600;
  color: var(--color-text-primary);
}

.feature-controls {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.feature-selects {
  display: flex;
  gap: 6px;
  align-items: center;
}

.feature-selects .field-select,
.feature-selects .ui-combobox {
  flex: 1;
  min-width: 0;
}

.btn-clear {
  width: 28px;
  height: 28px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 14px;
}

.btn-clear:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.actions {
  display: flex;
  justify-content: flex-end;
  margin-top: 4px;
}

@media (max-width: 760px) {
  .group-row__main {
    grid-template-columns: 1fr;
  }

  .feature-selects {
    flex-wrap: wrap;
  }

  .feature-selects .field-select,
  .feature-selects .ui-combobox {
    flex-basis: 160px;
  }
}

</style>
