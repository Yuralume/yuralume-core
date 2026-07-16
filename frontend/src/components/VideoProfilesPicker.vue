<script setup lang="ts">
import { ref, watch, onMounted, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { UiButton } from '@/components/ui'
import {
  listVideoProfiles,
  getActiveVideoProfilePreference,
  setActiveVideoProfilePreference,
  getVideoFeatureProfilePreferences,
  setVideoFeatureProfilePreferences,
  getCharacterVideoProfilePreferences,
  setCharacterVideoProfilePreferences,
  type VideoProfileSummary,
} from '@/utils/api/system'
import {
  featureKeyLabel,
  providerConnectionLabel,
  VIDEO_FEATURE_KEY_LABEL_NAMESPACE,
} from '@/utils/catalogLabels'

/** Picker for video-profile routing — sibling of ImageProfilesPicker.
 *
 * Same two-mode pattern (global / character) and same shape; the only
 * differences from the image side are: (a) the API endpoints, (b) the
 * media kind label, and (c) the empty-state hint pointing at
 * ``KOKORO_VIDEO_PROFILES`` rather than
 * the image variable. */

const props = defineProps<{
  characterId?: string
}>()

const { t } = useI18n()

const profiles = ref<VideoProfileSummary[]>([])
const knownKeys = ref<string[]>([])
const labels = ref<Record<string, string>>({})

const overrides = ref<Record<string, string | null>>({})
const activeProfileId = ref<string | null>(null)

const saving = ref(false)
const loaded = ref(false)

const profileById = computed<Record<string, VideoProfileSummary>>(() => {
  const out: Record<string, VideoProfileSummary> = {}
  for (const p of profiles.value) out[p.id] = p
  return out
})

const hasProfiles = computed(() => profiles.value.length > 0)

function profileLabel(id: string | null | undefined): string {
  if (!id) return ''
  const entry = profileById.value[id]
  if (!entry) return id
  const label = providerConnectionLabel(t, entry.label)
  if (entry.kind === 'external_api') return label
  // Kind label is informational for legacy local-dev profiles.
  const kindLabel =
    entry.kind === 'comfyui_wan22' ? 'Wan2.2' : entry.kind
  return t('common.labelWithKind', { label, kind: kindLabel })
}

async function loadAll() {
  loaded.value = false
  try {
    if (props.characterId) {
      const [profilesResp, charPrefs] = await Promise.all([
        listVideoProfiles(),
        getCharacterVideoProfilePreferences(props.characterId),
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
        listVideoProfiles(),
        getActiveVideoProfilePreference(),
        getVideoFeatureProfilePreferences(),
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
        ? t('videoProfilesPicker.errors.loadCharacterFailed')
        : t('videoProfilesPicker.errors.loadGlobalFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  }
}

/** Localized label for a video feature key. The backend still ships
 * the Chinese ``FEATURE_LABELS`` value as ``labels[key]``; route it
 * through the video picker's ``featureKeys`` namespace so non-zh UIs
 * stop leaking Chinese, falling back to the backend string for any
 * not-yet-translated key. */
function featureLabel(key: string): string {
  return featureKeyLabel(
    t,
    { key, label: labels.value[key] ?? key },
    VIDEO_FEATURE_KEY_LABEL_NAMESPACE,
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
      const result = await setCharacterVideoProfilePreferences(
        props.characterId, payload,
      )
      const merged: Record<string, string | null> = {}
      for (const key of knownKeys.value) {
        merged[key] = result.overrides[key]?.profile_id ?? null
      }
      overrides.value = merged
      notification.success({ message: t('videoProfilesPicker.savedCharacter'), duration: 2 })
    } else {
      const activeResp = await setActiveVideoProfilePreference({
        profile_id: activeProfileId.value,
      })
      activeProfileId.value = activeResp.profile_id

      const overridesPayload: Record<string, { profile_id: string | null }> = {}
      for (const [key, profileId] of Object.entries(overrides.value)) {
        if (!profileId) continue
        overridesPayload[key] = { profile_id: profileId }
      }
      const result = await setVideoFeatureProfilePreferences({
        overrides: overridesPayload,
        known_keys: knownKeys.value,
        labels: labels.value,
      })
      const merged: Record<string, string | null> = {}
      for (const key of knownKeys.value) {
        merged[key] = result.overrides[key]?.profile_id ?? null
      }
      overrides.value = merged
      notification.success({ message: t('videoProfilesPicker.savedGlobal'), duration: 2 })
    }
  } catch (error) {
    notification.error({
      message: t('videoProfilesPicker.errors.saveFailed'),
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
  <div class="video-profiles-picker">
    <p class="hint">
      <template v-if="characterId">
        {{ t('videoProfilesPicker.characterHintPrefix') }}
        <strong>{{ t('videoProfilesPicker.blank') }}</strong>{{ t('videoProfilesPicker.characterHintSuffix') }}
      </template>
      <template v-else>
        {{ t('videoProfilesPicker.globalHint') }}
      </template>
    </p>

    <div v-if="!loaded" class="loading-hint">{{ t('common.state.loading') }}</div>

    <div v-else-if="!hasProfiles" class="empty-hint">
      {{ t('videoProfilesPicker.emptyPrefix') }} <code>KOKORO_VIDEO_PROFILES</code>
      {{ t('videoProfilesPicker.emptySuffix') }}
    </div>

    <template v-else>
      <div v-if="!characterId" class="active-row">
        <div class="feature-label">{{ t('videoProfilesPicker.activeLabel') }}</div>
        <div class="feature-selects">
          <select
            v-model="activeProfileId"
            class="field-select"
          >
            <option :value="null">{{ t('videoProfilesPicker.useFirstProfile', { profile: profileLabel(profiles[0]?.id) }) }}</option>
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
                characterId ? t('videoProfilesPicker.inheritGlobal') : t('videoProfilesPicker.inheritDefault')
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
              :title="t('videoProfilesPicker.clearRowTitle')"
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
          : (characterId ? t('videoProfilesPicker.saveCharacter') : t('videoProfilesPicker.saveGlobal'))
      }}</UiButton>
    </div>
  </div>
</template>

<style scoped>
.video-profiles-picker {
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
