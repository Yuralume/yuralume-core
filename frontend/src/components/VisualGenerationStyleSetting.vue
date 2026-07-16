<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import {
  getVisualGenerationStylePreference,
  setVisualGenerationStylePreference,
  type VisualGenerationStyle,
} from '@/utils/api/system'

const { t } = useI18n()

const style = ref<VisualGenerationStyle>('anime')
const loaded = ref(false)
const saving = ref(false)

const options = computed(() => [
  {
    value: 'anime' as const,
    label: t('visualGenerationStyleSetting.options.anime.label'),
    hint: t('visualGenerationStyleSetting.options.anime.hint'),
  },
  {
    value: 'realistic' as const,
    label: t('visualGenerationStyleSetting.options.realistic.label'),
    hint: t('visualGenerationStyleSetting.options.realistic.hint'),
  },
])

const activeHint = computed(() => (
  options.value.find((option) => option.value === style.value)?.hint
  ?? t('visualGenerationStyleSetting.hint')
))

async function loadPreference() {
  loaded.value = false
  try {
    const pref = await getVisualGenerationStylePreference()
    style.value = pref.style
  } catch (error) {
    notification.error({
      message: t('visualGenerationStyleSetting.errors.loadFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    loaded.value = true
  }
}

async function handleChange(event: Event) {
  const target = event.target as HTMLSelectElement
  const next = target.value as VisualGenerationStyle
  const previous = style.value
  style.value = next
  saving.value = true
  try {
    const result = await setVisualGenerationStylePreference({ style: next })
    style.value = result.style
    notification.success({
      message: t('visualGenerationStyleSetting.notifications.switched'),
      duration: 2,
    })
  } catch (error) {
    style.value = previous
    notification.error({
      message: t('visualGenerationStyleSetting.errors.switchFailed'),
      description: error instanceof Error ? error.message : String(error),
      duration: 4,
    })
  } finally {
    saving.value = false
  }
}

onMounted(loadPreference)
</script>

<template>
  <div class="visual-generation-style-setting">
    <label class="field-label">{{ t('visualGenerationStyleSetting.label') }}</label>
    <select
      :value="style"
      class="field-select"
      :disabled="!loaded || saving"
      @change="handleChange"
    >
      <option
        v-for="option in options"
        :key="option.value"
        :value="option.value"
      >
        {{ option.label }}
      </option>
    </select>
    <p class="field-hint">{{ activeHint }}</p>
  </div>
</template>

<style scoped>
.visual-generation-style-setting {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
</style>
