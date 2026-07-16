<script setup lang="ts">
import { UiButton } from '@/components/ui'

defineProps<{
  icon?: string
  title: string
  hint?: string
  actionLabel?: string
}>()

const emit = defineEmits<{
  action: []
}>()
</script>

<template>
  <section class="player-empty-state">
    <div v-if="icon" class="player-empty-state__icon" aria-hidden="true">
      {{ icon }}
    </div>
    <h3 class="player-empty-state__title">{{ title }}</h3>
    <p v-if="hint" class="player-empty-state__hint">{{ hint }}</p>
    <UiButton
      v-if="actionLabel"
      variant="primary"
      size="sm"
      class="player-empty-state__action"
      @click="emit('action')"
    >
      {{ actionLabel }}
    </UiButton>
  </section>
</template>

<style scoped>
.player-empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 34px 18px;
  text-align: center;
  border: 1px dashed var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.025);
}

.player-empty-state__icon {
  font-size: 28px;
  color: var(--color-primary-light);
  opacity: 0.72;
}

.player-empty-state__title {
  margin: 0;
  font-size: 14px;
  font-weight: 650;
  line-height: 1.35;
  color: var(--color-text);
}

.player-empty-state__hint {
  max-width: 280px;
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.player-empty-state__action {
  margin-top: 4px;
}
</style>
