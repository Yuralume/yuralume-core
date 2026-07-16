<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    size?: 'md' | 'lg'
    hoverable?: boolean
    title?: string
    as?: string
  }>(),
  {
    size: 'md',
    hoverable: false,
    as: 'div',
  },
)

const classes = computed(() => [
  'ui-card',
  props.size === 'lg' && 'ui-card--lg',
  props.hoverable && 'ui-card--hoverable',
])
</script>

<template>
  <component :is="as" :class="classes">
    <header v-if="title || $slots.header" class="ui-card__header">
      <slot name="header">
        <h4 class="ui-card__title">{{ title }}</h4>
      </slot>
      <div v-if="$slots.actions" class="ui-card__actions">
        <slot name="actions" />
      </div>
    </header>
    <div class="ui-card__body">
      <slot />
    </div>
    <footer v-if="$slots.footer" class="ui-card__footer">
      <slot name="footer" />
    </footer>
  </component>
</template>

<style scoped>
.ui-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.ui-card__title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
  color: var(--color-text);
}
.ui-card__actions {
  display: flex;
  gap: var(--space-1);
  flex-shrink: 0;
}
.ui-card__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.ui-card__footer {
  margin-top: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-border);
}
</style>
