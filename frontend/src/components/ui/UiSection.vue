<script setup lang="ts">
withDefaults(
  defineProps<{
    title?: string
    description?: string
    bordered?: boolean
  }>(),
  {
    bordered: true,
  },
)
</script>

<template>
  <section :class="['ui-section', { 'ui-section--bordered': bordered }]">
    <header v-if="title || description || $slots.header" class="ui-section__header">
      <slot name="header">
        <h3 v-if="title" class="ui-section__title">{{ title }}</h3>
        <p v-if="description" class="ui-section__description">{{ description }}</p>
      </slot>
    </header>
    <div class="ui-section__body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.ui-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.ui-section--bordered {
  padding-bottom: var(--space-4);
  border-bottom: 1px solid var(--color-border);
}
.ui-section--bordered + .ui-section--bordered {
  padding-top: var(--space-4);
}
.ui-section__header {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.ui-section__title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
  color: var(--color-text);
}
.ui-section__description {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.ui-section__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
</style>
