<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import type { FusionStoryStatus } from '@/types/fusionStory'

const props = defineProps<{ status: FusionStoryStatus }>()
const { t } = useI18n()

const label = computed(() => {
  switch (props.status) {
    case 'planning':
      return t('fusionStory.status.planning')
    case 'writing':
      return t('fusionStory.status.writing')
    case 'polishing':
      return t('fusionStory.status.polishing')
    case 'ready':
      return t('fusionStory.status.ready')
    case 'failed':
      return t('fusionStory.status.failed')
  }
})

const tone = computed(() => {
  switch (props.status) {
    case 'ready':
      return 'success'
    case 'failed':
      return 'error'
    default:
      return 'progress'
  }
})
</script>

<template>
  <span class="badge" :data-tone="tone">{{ label }}</span>
</template>

<style scoped>
.badge {
  display: inline-block;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid transparent;
}
.badge[data-tone='progress'] {
  background: rgba(var(--color-primary-rgb), 0.18);
  color: var(--color-primary-light);
  border-color: rgba(var(--color-primary-rgb), 0.4);
}
.badge[data-tone='success'] {
  background: rgba(60, 125, 82, 0.18);
  color: #6db87d;
  border-color: rgba(60, 125, 82, 0.4);
}
.badge[data-tone='error'] {
  background: rgba(245, 34, 45, 0.18);
  color: var(--color-danger);
  border-color: rgba(245, 34, 45, 0.4);
}
</style>
