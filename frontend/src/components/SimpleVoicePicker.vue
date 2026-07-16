<script setup lang="ts">
/**
 * 玩家側的「簡易 TTS 選擇」單一下拉。
 *
 * 對每個角色提供：
 *   - 一個外部 voice catalog 下拉
 *   - 一個啟用 toggle
 *
 * 改了立刻 PATCH 角色，不需要 Save 按鈕。試聽、翻譯目標語等
 * 進階功能屬於 admin /admin/voice，這裡刻意不暴露。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import type { Character, VoiceProfile } from '@/types/character'
import { updateCharacter } from '@/utils/api/characters'
import { listTTSAssets, type TTSAssetCatalog } from '@/utils/api/ttsAssets'

const props = defineProps<{
  character: Character
}>()

const emit = defineEmits<{
  updated: [character: Character]
}>()

const { t } = useI18n()

const catalog = ref<TTSAssetCatalog | null>(null)
const catalogLoading = ref(false)
const saving = ref(false)

// 本地 form 跟著 character 同步；切換時 PATCH 出去由父層更新角色物件。
const enabled = ref<boolean>(props.character.voice_profile?.enabled ?? true)
const presetId = ref<string>(resolvePresetId(props.character.voice_profile))

const PRESET_NONE = ''

function emptyProfile(): VoiceProfile {
  return {
    enabled: true,
    voice_id: '',
    ref_audio_path: '',
    prompt_text: '',
    prompt_lang: '',
    translate_target_lang: '',
    gpt_weights_path: '',
    sovits_weights_path: '',
  }
}

function resolvePresetId(profile: VoiceProfile | null | undefined): string {
  if (!profile) return PRESET_NONE
  if (!profile.voice_id) return PRESET_NONE
  if (!catalog.value) {
    return profile.voice_id
  }
  const match = catalog.value.voice_presets.find(
    (p) => (p.voice_id || p.id) === profile.voice_id,
  )
  return match ? match.id : profile.voice_id
}

const presets = computed(() => catalog.value?.voice_presets ?? [])
const catalogEnabled = computed(() => catalog.value?.enabled === true)

async function loadCatalog() {
  catalogLoading.value = true
  try {
    catalog.value = await listTTSAssets()
    presetId.value = resolvePresetId(props.character.voice_profile)
  } catch {
    catalog.value = null
  } finally {
    catalogLoading.value = false
  }
}

onMounted(loadCatalog)

// 角色切換時把 form reset。
watch(
  () => props.character.id,
  () => {
    enabled.value = props.character.voice_profile?.enabled ?? true
    presetId.value = resolvePresetId(props.character.voice_profile)
  },
)

watch(
  () => props.character.voice_profile,
  (next) => {
    enabled.value = next?.enabled ?? true
    presetId.value = resolvePresetId(next)
  },
)

async function patchVoiceProfile(profile: VoiceProfile | null) {
  saving.value = true
  try {
    const updated = await updateCharacter(props.character.id, {
      voice_profile: profile,
    })
    emit('updated', updated)
  } catch (error) {
    notification.error({
      message: t('simpleVoicePicker.errors.saveFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
    // 回滾本地狀態到 prop 的值
    enabled.value = props.character.voice_profile?.enabled ?? true
    presetId.value = resolvePresetId(props.character.voice_profile)
  } finally {
    saving.value = false
  }
}

function buildProfileForPreset(id: string): VoiceProfile | null {
  if (id === PRESET_NONE) return null
  const preset = catalog.value?.voice_presets.find((p) => p.id === id)
  if (!preset) return props.character.voice_profile
  const base = props.character.voice_profile ?? emptyProfile()
  return {
    ...base,
    enabled: enabled.value,
    voice_id: preset.voice_id || preset.id,
    ref_audio_path: '',
    prompt_text: '',
    prompt_lang: preset.prompt_lang || '',
    gpt_weights_path: '',
    sovits_weights_path: '',
  }
}

async function handlePresetChange(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  presetId.value = value
  const profile = buildProfileForPreset(value)
  await patchVoiceProfile(profile)
}

async function handleEnabledChange(event: Event) {
  const next = (event.target as HTMLInputElement).checked
  enabled.value = next
  const current = props.character.voice_profile
  if (!current) {
    // 沒有 override → 建立一份只設 enabled 的空 profile
    await patchVoiceProfile({ ...emptyProfile(), enabled: next })
    return
  }
  await patchVoiceProfile({ ...current, enabled: next })
}
</script>

<template>
  <div class="simple-voice-picker">
    <label class="field-label">{{ t('simpleVoicePicker.label') }}</label>

    <div class="row">
      <select
        :value="presetId"
        class="field-select"
        :disabled="catalogLoading || saving || !catalogEnabled"
        @change="handlePresetChange"
      >
        <option :value="PRESET_NONE">{{ t('simpleVoicePicker.inheritGlobalOption') }}</option>
        <template v-if="catalogEnabled">
          <option
            v-for="preset in presets"
            :key="preset.id"
            :value="preset.id"
            :disabled="!preset.is_complete"
          >{{ preset.label }}</option>
        </template>
      </select>
    </div>

    <label class="enabled-row">
      <input
        type="checkbox"
        :checked="enabled"
        :disabled="saving"
        @change="handleEnabledChange"
      />
      <span>{{ t('simpleVoicePicker.enabledLabel') }}</span>
    </label>

    <p v-if="!catalogEnabled && !catalogLoading" class="field-hint field-hint--warn">
      {{ t('simpleVoicePicker.hints.catalogDisabled') }}
    </p>
    <p v-else class="field-hint">
      {{ t('simpleVoicePicker.hints.advanced') }}
    </p>
  </div>
</template>

<style scoped>
.simple-voice-picker {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.row {
  display: flex;
}
.enabled-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-sm);
  color: var(--color-text);
  cursor: pointer;
  margin-top: var(--space-1);
}
.field-hint--warn {
  color: #e6a23c;
}
</style>
