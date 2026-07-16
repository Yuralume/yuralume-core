<script setup lang="ts">
import { computed } from 'vue'

type Variant = 'default' | 'primary' | 'success' | 'warning' | 'danger'

const props = withDefaults(
  defineProps<{
    variant?: Variant
  }>(),
  {
    variant: 'default',
  },
)

const classes = computed(() => [
  'ui-badge',
  props.variant !== 'default' && `ui-badge--${props.variant}`,
])
</script>

<template>
  <span :class="classes">
    <slot />
  </span>
</template>
