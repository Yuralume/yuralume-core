<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import CharacterLorasPanel from '@/components/CharacterLorasPanel.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiCard, UiBadge } from '@/components/ui'

const { t } = useI18n()
</script>

<template>
  <div class="loras-admin">
    <header class="loras-admin__header">
      <div>
        <h1>{{ t('admin.page.loras.title') }}</h1>
        <p class="loras-admin__subtitle">
          {{ t('admin.page.loras.subtitle') }}
        </p>
      </div>
      <div class="admin-header-badges">
        <UiBadge variant="warning">{{ t('admin.page.deploymentBoundBadge') }}</UiBadge>
        <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
      </div>
    </header>

    <AdminCharacterPicker
      :hint="t('admin.page.loras.pickerHint')"
    >
      <template #default="{ character, patch }">
        <UiCard size="lg">
          <template #header>
            <h2 class="loras-admin__card-title">{{ t('admin.page.loras.cardTitle', { name: character.name }) }}</h2>
          </template>

          <CharacterLorasPanel :character="character" @updated="patch" />
        </UiCard>
      </template>
    </AdminCharacterPicker>
  </div>
</template>

<style scoped>
.loras-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.loras-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.loras-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.loras-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.loras-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
</style>
