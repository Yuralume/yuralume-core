<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import WorldAdminEditor from '@/components/admin/WorldAdminEditor.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiBadge } from '@/components/ui'

const { t } = useI18n()
</script>

<template>
  <div class="world-admin">
    <header class="world-admin__header">
      <div>
        <h1>{{ t('admin.page.world.title') }}</h1>
        <p class="world-admin__subtitle">
          {{ t('admin.page.world.subtitle') }}
        </p>
      </div>
      <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
    </header>

    <AdminCharacterPicker
      :hint="t('admin.page.world.pickerHint')"
    >
      <template #default="{ character, patch }">
        <!--
          ``:key`` forces a fresh mount whenever the picker switches character,
          so WorldAdminEditor's setup() re-snapshots the form from the new
          character without needing a manual watcher. Cleaner than reactive
          re-sync inside the editor.
        -->
        <WorldAdminEditor
          :key="character.id"
          :character="character"
          :patch="patch"
        />
      </template>
    </AdminCharacterPicker>
  </div>
</template>

<style scoped>
.world-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.world-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.world-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.world-admin__subtitle {
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
