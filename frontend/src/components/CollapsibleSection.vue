<script setup lang="ts">
import { ref, watch } from 'vue'

const props = withDefaults(defineProps<{
  title: string
  hint?: string
  defaultOpen?: boolean
  openSignal?: number
}>(), {
  hint: '',
  defaultOpen: true,
  openSignal: 0,
})

const open = ref(props.defaultOpen || props.openSignal > 0)

watch(() => props.openSignal, (next, prev) => {
  if (next !== prev && next > 0) {
    open.value = true
  }
})
</script>

<template>
  <section :class="['collapsible', { open }]">
    <button
      type="button"
      class="collapsible-header"
      :aria-expanded="open"
      @click="open = !open"
    >
      <span class="collapsible-caret">{{ open ? '▾' : '▸' }}</span>
      <span class="collapsible-title">{{ title }}</span>
      <span v-if="hint" class="collapsible-hint">{{ hint }}</span>
    </button>
    <div v-show="open" class="collapsible-body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.collapsible {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  margin-bottom: 12px;
}

.collapsible-header {
  display: flex;
  align-items: baseline;
  gap: 8px;
  width: 100%;
  padding: 12px 14px;
  background: transparent;
  border: 0;
  color: var(--color-text-primary);
  font-size: 14px;
  font-weight: 600;
  text-align: left;
  cursor: pointer;
}

.collapsible-header:hover {
  background: var(--color-surface-hover, rgba(255, 255, 255, 0.03));
}

.collapsible-caret {
  width: 12px;
  color: var(--color-text-secondary);
}

.collapsible-title {
  flex: 0 0 auto;
}

.collapsible-hint {
  flex: 1 1 auto;
  font-size: 12px;
  font-weight: 400;
  color: var(--color-text-secondary);
  text-align: right;
}

.collapsible-body {
  padding: 4px 14px 14px;
  border-top: 1px solid var(--color-border);
}
</style>
