<script setup lang="ts">
import { ref, watch } from 'vue'
import type { Character } from '@/types/character'
import { updateCharacter } from '@/utils/api/characters'
import { UiCard, UiButton } from '@/components/ui'
import { useI18n } from 'vue-i18n'

/**
 * Per-character editor for proactive-messaging knobs (enabled / daily limit
 * / cooldown) plus the LumeGram feed daily quota — sidebar settings tab
 * grouped these two together so we keep the grouping here.
 *
 * Quiet hours / busy-defer thresholds are *backend-global* env knobs, not
 * per-character columns; they intentionally don't appear here. If/when
 * those graduate to per-character overrides on the Character DTO, extend
 * this form rather than spawning a new page.
 */
const props = defineProps<{
  character: Character
  patch: (updated: Character) => void
}>()

const { t } = useI18n()

interface ProactiveForm {
  proactive_enabled: boolean
  proactive_daily_limit: number
  proactive_cooldown_minutes: number
  feed_daily_limit: number
}

function snapshot(char: Character): ProactiveForm {
  return {
    proactive_enabled: char.proactive_enabled ?? true,
    proactive_daily_limit: char.proactive_daily_limit ?? 3,
    proactive_cooldown_minutes: char.proactive_cooldown_minutes ?? 30,
    feed_daily_limit: char.feed_daily_limit ?? 3,
  }
}

const form = ref<ProactiveForm>(snapshot(props.character))
const saving = ref(false)
const errorMsg = ref<string | null>(null)
const successMsg = ref<string | null>(null)

async function handleSave() {
  saving.value = true
  errorMsg.value = null
  successMsg.value = null
  try {
    const updated = await updateCharacter(props.character.id, {
      proactive_enabled: form.value.proactive_enabled,
      proactive_daily_limit: form.value.proactive_daily_limit,
      proactive_cooldown_minutes: form.value.proactive_cooldown_minutes,
      feed_daily_limit: form.value.feed_daily_limit,
    })
    props.patch(updated)
    successMsg.value = t('admin.proactiveEditor.saved')
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('admin.proactiveEditor.saveFailed')
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
  <div class="proactive-editor">
    <UiCard size="lg">
      <template #header>
        <h2 class="proactive-editor__card-title">
          {{ t('admin.proactiveEditor.proactiveTitle', { name: character.name }) }}
        </h2>
      </template>

      <p class="proactive-editor__note">
        {{ t('admin.proactiveEditor.proactiveNote') }}
      </p>

      <label class="field-label proactive-editor__toggle">
        <input v-model="form.proactive_enabled" type="checkbox" />
        <span>{{ t('admin.proactiveEditor.enableProactive') }}</span>
      </label>

      <div class="proactive-editor__grid">
        <div class="proactive-editor__field">
          <label class="field-label">{{ t('admin.proactiveEditor.dailyLimit') }}</label>
          <input
            v-model.number="form.proactive_daily_limit"
            type="number"
            min="0"
            max="50"
            class="field-input"
          />
          <span class="proactive-editor__suffix">{{ t('admin.proactiveEditor.messageUnit') }}</span>
        </div>
        <div class="proactive-editor__field">
          <label class="field-label">{{ t('admin.proactiveEditor.cooldownMinutes') }}</label>
          <input
            v-model.number="form.proactive_cooldown_minutes"
            type="number"
            min="1"
            max="1440"
            class="field-input"
          />
          <span class="proactive-editor__suffix">min</span>
        </div>
      </div>
    </UiCard>

    <UiCard size="lg">
      <template #header>
        <h2 class="proactive-editor__card-title">{{ t('admin.proactiveEditor.feedTitle') }}</h2>
      </template>

      <p class="proactive-editor__note">
        {{ t('admin.proactiveEditor.feedNotePrefix') }} <code>0</code>
        {{ t('admin.proactiveEditor.feedNoteSuffix') }}
      </p>

      <div class="proactive-editor__grid">
        <div class="proactive-editor__field">
          <label class="field-label">{{ t('admin.proactiveEditor.feedDailyLimit') }}</label>
          <input
            v-model.number="form.feed_daily_limit"
            type="number"
            min="0"
            max="50"
            class="field-input"
          />
          <span class="proactive-editor__suffix">{{ t('admin.proactiveEditor.postUnit') }}</span>
        </div>
      </div>
    </UiCard>

    <p class="proactive-editor__hint">
      {{ t('admin.proactiveEditor.globalHintPrefix') }} <code>.env</code>
      {{ t('admin.proactiveEditor.globalHintSuffix') }}
    </p>

    <div class="proactive-editor__actions">
      <div class="proactive-editor__status">
        <span v-if="errorMsg" class="proactive-editor__error">{{ errorMsg }}</span>
        <span v-else-if="successMsg" class="proactive-editor__success">{{ successMsg }}</span>
      </div>
      <UiButton
        variant="primary"
        :loading="saving"
        :disabled="saving"
        @click="handleSave"
      >{{ saving ? t('common.state.saving') : t('admin.proactiveEditor.saveAction') }}</UiButton>
    </div>
  </div>
</template>

<style scoped>
.proactive-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.proactive-editor__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.proactive-editor__note {
  margin: 0 0 var(--space-3);
  padding: var(--space-2) var(--space-3);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  line-height: 1.6;
}
.proactive-editor__toggle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-3);
}
.proactive-editor__grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: var(--space-3);
}
.proactive-editor__field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.proactive-editor__suffix {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.proactive-editor__hint {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.proactive-editor__actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
}
.proactive-editor__status {
  flex: 1;
  font-size: var(--font-sm);
}
.proactive-editor__error {
  color: #f4a3a3;
}
.proactive-editor__success {
  color: #6dd58c;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
}
</style>
