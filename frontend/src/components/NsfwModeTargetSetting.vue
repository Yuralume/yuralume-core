<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import {
  getAdminNsfwModeTarget,
  listImageProfiles,
  listProviderModels,
  setAdminNsfwModeTarget,
  type ImageProfileSummary,
  type NsfwModeTarget,
} from '@/utils/api/system'
import { UiButton, UiCombobox } from '@/components/ui'
import { providerConnectionLabel } from '@/utils/catalogLabels'

const props = defineProps<{
  providers: string[]
  providersLoaded: boolean
}>()

const { t } = useI18n()

const loaded = ref(false)
const saving = ref(false)
const modelsLoading = ref(false)
const models = ref<string[]>([])
const imageProfiles = ref<ImageProfileSummary[]>([])
const selectedProviderId = ref('')
const selectedModelId = ref('')
const selectedImageProfileId = ref('')
const configured = ref(false)

const hasSelectableTarget = computed(() => (
  selectedProviderId.value.length > 0
  && selectedModelId.value.length > 0
  && selectedImageProfileId.value.length > 0
))

function applyTarget(nextTarget: NsfwModeTarget | null): void {
  if (!nextTarget) return
  selectedProviderId.value = nextTarget.llm_provider_id
  selectedModelId.value = nextTarget.llm_model_id
  selectedImageProfileId.value = nextTarget.image_profile_id
}

async function loadModels(providerId: string, preferredModelId: string | null = null): Promise<void> {
  models.value = []
  if (!providerId) {
    selectedModelId.value = ''
    return
  }
  modelsLoading.value = true
  try {
    const nextModels = await listProviderModels(providerId)
    models.value = nextModels
    selectedModelId.value = preferredModelId && nextModels.includes(preferredModelId)
      ? preferredModelId
      : nextModels[0] ?? ''
  } catch (error) {
    selectedModelId.value = ''
    notification.error({
      message: t('nsfwModeTargetSetting.errors.modelsLoadFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    modelsLoading.value = false
  }
}

async function loadAll(): Promise<void> {
  if (!props.providersLoaded) return
  loaded.value = false
  try {
    const [profileList, pref] = await Promise.all([
      listImageProfiles(),
      getAdminNsfwModeTarget(),
    ])
    imageProfiles.value = profileList
    configured.value = pref.configured
    const existingTarget = pref.target
    if (existingTarget) {
      applyTarget(existingTarget)
    } else {
      selectedProviderId.value = props.providers[0] ?? ''
      selectedImageProfileId.value = profileList[0]?.id ?? ''
    }
    await loadModels(selectedProviderId.value, existingTarget?.llm_model_id ?? null)
  } catch (error) {
    notification.error({
      message: t('nsfwModeTargetSetting.errors.loadFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    loaded.value = true
  }
}

async function handleProviderChange(event: Event): Promise<void> {
  const providerId = (event.target as HTMLSelectElement).value
  selectedProviderId.value = providerId
  await loadModels(providerId)
}

async function saveTarget(): Promise<void> {
  if (!hasSelectableTarget.value) {
    notification.warning({
      message: t('nsfwModeTargetSetting.errors.targetRequired'),
      duration: 3,
    })
    return
  }
  saving.value = true
  try {
    const result = await setAdminNsfwModeTarget({
      llm_provider_id: selectedProviderId.value,
      llm_model_id: selectedModelId.value,
      image_profile_id: selectedImageProfileId.value,
    })
    configured.value = result.configured
    applyTarget(result.target)
    notification.success({
      message: t('nsfwModeTargetSetting.notifications.saved'),
      duration: 2,
    })
  } catch (error) {
    notification.error({
      message: t('nsfwModeTargetSetting.errors.saveFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    saving.value = false
  }
}

onMounted(loadAll)

watch(() => props.providersLoaded, (next) => {
  if (next && !loaded.value) {
    void loadAll()
  }
})
</script>

<template>
  <div class="nsfw-mode-target-setting">
    <p class="hint">{{ t('nsfwModeTargetSetting.hint') }}</p>

    <div v-if="!loaded || !providersLoaded" class="loading-hint">
      {{ t('common.state.loading') }}
    </div>

    <div v-else class="target-grid">
      <label class="target-field">
        <span class="field-label">{{ t('nsfwModeTargetSetting.fields.provider') }}</span>
        <select
          v-model="selectedProviderId"
          class="field-select"
          :disabled="saving || providers.length === 0"
          @change="handleProviderChange"
        >
          <option v-if="providers.length === 0" value="">
            {{ t('nsfwModeTargetSetting.empty.providers') }}
          </option>
          <option v-for="provider in providers" :key="provider" :value="provider">
            {{ provider }}
          </option>
        </select>
      </label>

      <label class="target-field">
        <span class="field-label">{{ t('nsfwModeTargetSetting.fields.model') }}</span>
        <UiCombobox
          v-model="selectedModelId"
          :options="models"
          :loading="modelsLoading"
          :disabled="saving"
          :placeholder="models.length === 0 && !modelsLoading
            ? t('nsfwModeTargetSetting.empty.models')
            : t('nsfwModeTargetSetting.fields.model')"
          :aria-label="t('nsfwModeTargetSetting.fields.model')"
        />
      </label>

      <label class="target-field">
        <span class="field-label">{{ t('nsfwModeTargetSetting.fields.imageProfile') }}</span>
        <select
          v-model="selectedImageProfileId"
          class="field-select"
          :disabled="saving || imageProfiles.length === 0"
        >
          <option v-if="imageProfiles.length === 0" value="">
            {{ t('nsfwModeTargetSetting.empty.imageProfiles') }}
          </option>
          <option
            v-for="profile in imageProfiles"
            :key="profile.id"
            :value="profile.id"
          >
            {{ providerConnectionLabel(t, profile.label) }}
          </option>
        </select>
      </label>
    </div>

    <div class="target-actions">
      <span
        class="target-state"
        :class="{ 'is-configured': configured }"
      >
        {{
          configured
            ? t('nsfwModeTargetSetting.status.configured')
            : t('nsfwModeTargetSetting.status.unconfigured')
        }}
      </span>
      <UiButton
        variant="primary"
        :loading="saving"
        :disabled="!loaded || !hasSelectableTarget"
        @click="saveTarget"
      >
        {{ t('nsfwModeTargetSetting.actions.save') }}
      </UiButton>
    </div>
  </div>
</template>

<style scoped>
.nsfw-mode-target-setting {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.hint,
.loading-hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.5;
}

.target-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}

.target-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.target-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.target-state {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.target-state.is-configured {
  color: #b8c2d6;
}

@media (max-width: 820px) {
  .target-grid {
    grid-template-columns: 1fr;
  }
}
</style>
