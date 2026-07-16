<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import SchedulePanel from '@/components/SchedulePanel.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiBadge } from '@/components/ui'

const { t } = useI18n()

/**
 * SchedulePanel already handles its own CRUD + reload-on-character-switch
 * via ``watch(() => props.characterId)``, so we don't need an inner-editor
 * wrapper here — just feed the picked character.id straight through.
 *
 * Player route still shows a read-only simplified card; the full editor
 * lives here (memorialized lockout, 3-day rolling window, regenerate
 * button etc. all stay in the original panel).
 */
</script>

<template>
  <div class="schedule-admin">
    <header class="schedule-admin__header">
      <div>
        <h1>{{ t('admin.page.schedule.title') }}</h1>
        <p class="schedule-admin__subtitle">
          {{ t('admin.page.schedule.subtitle') }}
        </p>
      </div>
      <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
    </header>

    <AdminCharacterPicker
      :hint="t('admin.page.schedule.pickerHint')"
    >
      <template #default="{ character }">
        <SchedulePanel :character-id="character.id" />
      </template>
    </AdminCharacterPicker>
  </div>
</template>

<style scoped>
.schedule-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.schedule-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.schedule-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.schedule-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
}
</style>
