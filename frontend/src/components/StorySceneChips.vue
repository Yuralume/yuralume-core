<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'
import type { StorySceneSuggestedAction } from '@/types/storyScene'

/**
 * Suggested actions inside a running scene (plan SC, decision #1).
 *
 * The chips answer "I am in the scene and still do not want to think", and
 * they answer it the way every other suggestion in this app does: pressing
 * one *fills the box*, it does not send. The player still edits, still adds,
 * still deletes it and writes their own — the chip is a starting line, never
 * a multiple-choice question. That distinction is the whole reason this is a
 * roleplay scene rather than a branching-drama node.
 */
defineProps<{
  actions: StorySceneSuggestedAction[]
  /** A turn is in flight — the suggestions belong to the previous one. */
  disabled?: boolean
}>()

const emit = defineEmits<{
  select: [text: string]
}>()

const { t } = useI18n()
</script>

<template>
  <div
    v-if="actions.length > 0"
    class="scene-chips"
    role="group"
    :aria-label="t('chat.storyScene.chipsAria')"
  >
    <UiButton
      v-for="(action, index) in actions"
      :key="index"
      variant="chip"
      size="sm"
      class="scene-chips__item"
      :disabled="disabled"
      @click="emit('select', action.text)"
    >
      {{ action.text }}
    </UiButton>
    <span class="scene-chips__hint">{{ t('chat.storyScene.chipsHint') }}</span>
  </div>
</template>

<style scoped>
.scene-chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.scene-chips__item {
  max-width: 100%;
  white-space: normal;
  text-align: left;
  line-height: 1.35;
  border-color: rgba(var(--color-primary-rgb), 0.34);
  background: rgba(var(--color-primary-rgb), 0.1);
}

.scene-chips__hint {
  flex: 1 0 auto;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.35;
}
</style>
