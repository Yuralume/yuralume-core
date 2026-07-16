<script setup lang="ts">
/**
 * 玩家側的「主要生圖通道」單一下拉。
 *
 * 只控制全域 active image profile —— 改了會立刻 PUT 後端，
 * 不需要 Save 按鈕。per-feature / per-character override 屬於 admin
 * 範疇，這裡刻意不暴露。完整路由請去 /admin/image-profiles。
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import {
  getActiveImageProfilePreference,
  listImageProfiles,
  setActiveImageProfilePreference,
  type ImageProfileSummary,
} from '@/utils/api/system'
import { providerConnectionLabel } from '@/utils/catalogLabels'

const { t } = useI18n()

const profiles = ref<ImageProfileSummary[]>([])
const activeProfileId = ref<string | null>(null)
const loaded = ref(false)
const saving = ref(false)

const hasProfiles = computed(() => profiles.value.length > 0)

function labelFor(p: ImageProfileSummary): string {
  // Re-localize a frozen "<provider> — <capability>" label so the player
  // dropdown never shows e.g. "OpenAI — 生圖" in a non-Chinese UI.
  return providerConnectionLabel(t, p.label)
}

async function loadAll() {
  loaded.value = false
  try {
    const [profilesResp, activePref] = await Promise.all([
      listImageProfiles(),
      getActiveImageProfilePreference(),
    ])
    profiles.value = profilesResp
    activeProfileId.value = activePref.profile_id
  } catch (error) {
    notification.error({
      message: t('simpleImageProfilePicker.errors.loadFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    loaded.value = true
  }
}

async function handleChange(event: Event) {
  const target = event.target as HTMLSelectElement
  const value = target.value || null
  const previous = activeProfileId.value
  activeProfileId.value = value
  saving.value = true
  try {
    const result = await setActiveImageProfilePreference({ profile_id: value })
    activeProfileId.value = result.profile_id
    notification.success({ message: t('simpleImageProfilePicker.notifications.switched'), duration: 2 })
  } catch (error) {
    activeProfileId.value = previous
    notification.error({
      message: t('simpleImageProfilePicker.errors.switchFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    saving.value = false
  }
}

onMounted(loadAll)
</script>

<template>
  <div class="simple-image-profile-picker">
    <label class="field-label">{{ t('simpleImageProfilePicker.label') }}</label>
    <select
      :value="activeProfileId ?? ''"
      class="field-select"
      :disabled="!loaded || !hasProfiles || saving"
      @change="handleChange"
    >
      <option v-if="!loaded" value="">{{ t('common.state.loading') }}</option>
      <option v-else-if="!hasProfiles" value="">{{ t('simpleImageProfilePicker.emptyProfiles') }}</option>
      <template v-else>
        <option value="">{{ t('simpleImageProfilePicker.useFirstProfile', { profile: labelFor(profiles[0]) }) }}</option>
        <option
          v-for="p in profiles"
          :key="p.id"
          :value="p.id"
        >{{ labelFor(p) }}</option>
      </template>
    </select>
    <p class="field-hint">
      {{ t('simpleImageProfilePicker.hint') }}
    </p>
  </div>
</template>

<style scoped>
.simple-image-profile-picker {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
</style>
