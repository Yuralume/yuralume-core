<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import ProactiveAdminEditor from '@/components/admin/ProactiveAdminEditor.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiBadge } from '@/components/ui'

const { t } = useI18n()
</script>

<template>
  <div class="proactive-admin">
    <header class="proactive-admin__header">
      <div>
        <h1>{{ t('admin.page.proactive.title') }}</h1>
        <p class="proactive-admin__subtitle">
          {{ t('admin.page.proactive.subtitle') }}
        </p>
      </div>
      <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
    </header>

    <AdminCharacterPicker
      :hint="t('admin.page.proactive.pickerHint')"
    >
      <template #default="{ character, patch }">
        <ProactiveAdminEditor
          :key="character.id"
          :character="character"
          :patch="patch"
        />
      </template>
    </AdminCharacterPicker>
  </div>
</template>

<style scoped>
.proactive-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 900px;
}
.proactive-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.proactive-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.proactive-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
</style>
