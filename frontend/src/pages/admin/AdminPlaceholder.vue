<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { UiCard, UiBadge } from '@/components/ui'

defineProps<{
  title: string
  subtitle?: string
  panelName: string
  status?: 'planned' | 'in-progress'
  notes?: string[]
}>()

const { t } = useI18n()
</script>

<template>
  <div class="admin-placeholder">
    <header class="admin-placeholder__header">
      <h1>{{ title }}</h1>
      <UiBadge :variant="status === 'in-progress' ? 'warning' : 'default'">
        {{ status === 'in-progress' ? t('admin.home.statusInProgress') : t('admin.placeholder.comingSoon') }}
      </UiBadge>
    </header>
    <p v-if="subtitle" class="admin-placeholder__subtitle">{{ subtitle }}</p>

    <UiCard :title="t('admin.placeholder.shellTitle')" size="lg">
      <i18n-t keypath="admin.placeholder.body" tag="p">
        <template #panel>
          <code>{{ panelName }}</code>
        </template>
      </i18n-t>
      <ul v-if="notes && notes.length" class="admin-placeholder__notes">
        <li v-for="n in notes" :key="n">{{ n }}</li>
      </ul>
      <template #footer>
        <p class="admin-placeholder__hint">
          {{ t('admin.placeholder.footerHint') }}
        </p>
      </template>
    </UiCard>
  </div>
</template>

<style scoped>
.admin-placeholder {
  max-width: 880px;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.admin-placeholder__header {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}
.admin-placeholder__header h1 {
  margin: 0;
  font-size: var(--font-xl);
}
.admin-placeholder__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.admin-placeholder__notes {
  margin: var(--space-2) 0 0;
  padding-left: var(--space-4);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.7;
}
.admin-placeholder__hint {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
  color: var(--color-text);
}
</style>
