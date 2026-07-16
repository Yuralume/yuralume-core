<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { UiButton } from '@/components/ui'
import {
  listImageProfiles,
  getActiveImageProfilePreference,
  setActiveImageProfilePreference,
  getImageFeatureProfilePreferences,
  setImageFeatureProfilePreferences,
  getCharacterImageProfilePreferences,
  setCharacterImageProfilePreferences,
  type ImageProfileSummary,
} from '@/utils/api/system'
import {
  featureKeyLabel,
  IMAGE_FEATURE_KEY_LABEL_NAMESPACE,
  providerConnectionLabel,
} from '@/utils/catalogLabels'

/** Picker for image-profile routing.
 *
 * Two modes (mirrors FeatureModelsPicker exactly):
 *   - Global (no characterId): picks the global active profile +
 *     per-feature overrides written to ``image_feature_profiles``.
 *   - Character (characterId set): per-character per-feature overrides
 *     stored on the character row; falls through to global when blank.
 *
 * Profiles themselves are operator-declared via
 * ``KOKORO_IMAGE_PROFILES`` JSON — the picker just reads the list. */

const props = defineProps<{
  characterId?: string
}>()

const { t } = useI18n()

const profiles = ref<ImageProfileSummary[]>([])
const knownKeys = ref<string[]>([])
const labels = ref<Record<string, string>>({})

/** Per-feature picks. ``null`` = inherit from the next layer up. */
const overrides = ref<Record<string, string | null>>({})

/** Global mode only — the global "active profile" pick. */
const activeProfileId = ref<string | null>(null)

const saving = ref(false)
const loaded = ref(false)

const profileById = computed<Record<string, ImageProfileSummary>>(() => {
  const out: Record<string, ImageProfileSummary> = {}
  for (const p of profiles.value) out[p.id] = p
  return out
})

const hasProfiles = computed(() => profiles.value.length > 0)

function profileLabel(id: string | null | undefined): string {
  if (!id) return ''
  const entry = profileById.value[id]
  if (!entry) return id  // stale pick — show raw id so operator notices
  const label = providerConnectionLabel(t, entry.label)
  if (entry.kind === 'external_api') return label
  return entry.kind === 'comfyui'
    ? t('common.labelWithKind', { label, kind: 'ComfyUI' })
    : t('common.labelWithKind', { label, kind: 'OpenAI' })
}

async function loadAll() {
  loaded.value = false
  try {
    // Profile catalogue is shared between global + character modes.
    // Fetch in parallel so the panel renders quickly.
    if (props.characterId) {
      const [profilesResp, charPrefs] = await Promise.all([
        listImageProfiles(),
        getCharacterImageProfilePreferences(props.characterId),
      ])
      profiles.value = profilesResp
      knownKeys.value = charPrefs.known_keys
      labels.value = charPrefs.labels
      const merged: Record<string, string | null> = {}
      for (const key of charPrefs.known_keys) {
        merged[key] = charPrefs.overrides[key]?.profile_id ?? null
      }
      overrides.value = merged
    } else {
      const [profilesResp, activePref, featurePrefs] = await Promise.all([
        listImageProfiles(),
        getActiveImageProfilePreference(),
        getImageFeatureProfilePreferences(),
      ])
      profiles.value = profilesResp
      activeProfileId.value = activePref.profile_id
      knownKeys.value = featurePrefs.known_keys
      labels.value = featurePrefs.labels
      const merged: Record<string, string | null> = {}
      for (const key of featurePrefs.known_keys) {
        merged[key] = featurePrefs.overrides[key]?.profile_id ?? null
      }
      overrides.value = merged
    }
    loaded.value = true
  } catch (error) {
    notification.error({
      message: props.characterId
        ? t('imageProfilesPicker.errors.loadCharacterFailed')
        : t('imageProfilesPicker.errors.loadGlobalFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  }
}

/** Localized label for an image feature key. The backend still ships
 * the Chinese ``FEATURE_LABELS`` value as ``labels[key]``; route it
 * through the image picker's ``featureKeys`` namespace so non-zh UIs
 * stop leaking Chinese, falling back to the backend string for any
 * not-yet-translated key. */
function featureLabel(key: string): string {
  return featureKeyLabel(
    t,
    { key, label: labels.value[key] ?? key },
    IMAGE_FEATURE_KEY_LABEL_NAMESPACE,
  )
}

function clearRow(key: string) {
  overrides.value[key] = null
}

function onOverrideChange(key: string, value: string) {
  overrides.value[key] = value || null
}

async function handleSave() {
  saving.value = true
  try {
    if (props.characterId) {
      const payload: Record<string, { feature_key: string; profile_id: string | null }> = {}
      for (const [key, profileId] of Object.entries(overrides.value)) {
        if (!profileId) continue
        payload[key] = { feature_key: key, profile_id: profileId }
      }
      const result = await setCharacterImageProfilePreferences(
        props.characterId, payload,
      )
      const merged: Record<string, string | null> = {}
      for (const key of knownKeys.value) {
        merged[key] = result.overrides[key]?.profile_id ?? null
      }
      overrides.value = merged
      notification.success({ message: t('imageProfilesPicker.savedCharacter'), duration: 2 })
    } else {
      // Persist active + per-feature in two PUTs. We could sequence
      // them — but doing both as one optimistic block keeps the UI's
      // "saving…" indicator single-pulse instead of flickering twice.
      const activeResp = await setActiveImageProfilePreference({
        profile_id: activeProfileId.value,
      })
      activeProfileId.value = activeResp.profile_id

      const overridesPayload: Record<string, { profile_id: string | null }> = {}
      for (const [key, profileId] of Object.entries(overrides.value)) {
        if (!profileId) continue
        overridesPayload[key] = { profile_id: profileId }
      }
      const result = await setImageFeatureProfilePreferences({
        overrides: overridesPayload,
        known_keys: knownKeys.value,
        labels: labels.value,
      })
      const merged: Record<string, string | null> = {}
      for (const key of knownKeys.value) {
        merged[key] = result.overrides[key]?.profile_id ?? null
      }
      overrides.value = merged
      notification.success({ message: t('imageProfilesPicker.savedGlobal'), duration: 2 })
    }
  } catch (error) {
    notification.error({
      message: t('imageProfilesPicker.errors.saveFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    saving.value = false
  }
}

onMounted(loadAll)

watch(() => props.characterId, () => {
  void loadAll()
})
</script>

<template>
  <div class="image-profiles-picker">
    <p class="hint">
      <template v-if="characterId">
        {{ t('imageProfilesPicker.characterHintPrefix') }}
        <strong>{{ t('imageProfilesPicker.blank') }}</strong>{{ t('imageProfilesPicker.characterHintSuffix') }}
      </template>
      <template v-else>
        {{ t('imageProfilesPicker.globalHintPrefix') }}
        <strong>{{ t('imageProfilesPicker.globalDefault') }}</strong>{{ t('imageProfilesPicker.globalHintSuffix') }}
      </template>
    </p>

    <div v-if="!loaded" class="loading-hint">{{ t('common.state.loading') }}</div>

    <div v-else-if="!hasProfiles" class="empty-hint">
      {{ t('imageProfilesPicker.emptyPrefix') }} <code>KOKORO_IMAGE_PROFILES</code>
      {{ t('imageProfilesPicker.emptyMiddle') }} <code>KOKORO_COMFYUI_*</code> /
      <code>KOKORO_OPENAI_IMAGE_*</code> {{ t('imageProfilesPicker.emptySuffix') }}
    </div>

    <template v-else>
      <div v-if="!characterId" class="active-row">
        <div class="feature-label">{{ t('imageProfilesPicker.activeLabel') }}</div>
        <div class="feature-selects">
          <select
            v-model="activeProfileId"
            class="field-select"
          >
            <option :value="null">{{ t('imageProfilesPicker.useFirstProfile', { profile: profileLabel(profiles[0]?.id) }) }}</option>
            <option
              v-for="p in profiles"
              :key="p.id"
              :value="p.id"
            >{{ profileLabel(p.id) }}</option>
          </select>
        </div>
      </div>

      <div class="feature-rows">
        <div
          v-for="key in knownKeys"
          :key="key"
          class="feature-row"
        >
          <div class="feature-label">{{ featureLabel(key) }}</div>
          <div class="feature-selects">
            <select
              :value="overrides[key] ?? ''"
              class="field-select"
              @change="onOverrideChange(key, ($event.target as HTMLSelectElement).value)"
            >
              <option value="">{{
                characterId ? t('imageProfilesPicker.inheritGlobal') : t('imageProfilesPicker.inheritDefault')
              }}</option>
              <option
                v-for="p in profiles"
                :key="p.id"
                :value="p.id"
              >{{ profileLabel(p.id) }}</option>
            </select>
            <button
              v-if="overrides[key]"
              type="button"
              class="btn-clear"
              :title="t('imageProfilesPicker.clearRowTitle')"
              @click="clearRow(key)"
            >×</button>
          </div>
        </div>
      </div>
    </template>

    <div class="actions">
      <UiButton
        variant="primary"
        :loading="saving"
        :disabled="!loaded || !hasProfiles"
        @click="handleSave"
      >{{
        saving
          ? t('common.state.saving')
          : (characterId ? t('imageProfilesPicker.saveCharacter') : t('imageProfilesPicker.saveGlobal'))
      }}</UiButton>
    </div>
  </div>
</template>

<style scoped>
.image-profiles-picker {
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

.loading-hint,
.empty-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
  padding: 12px 0;
  line-height: 1.5;
}

.empty-hint code {
  background: var(--color-surface-2, rgba(0, 0, 0, 0.04));
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 11px;
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
  gap: 8px;
}

.feature-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.feature-label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.feature-selects {
  display: flex;
  gap: 6px;
  align-items: center;
}

.feature-selects .field-select {
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

</style>
