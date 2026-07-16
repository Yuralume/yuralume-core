<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import VoiceProfilePanel from '@/components/VoiceProfilePanel.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiCard, UiBadge } from '@/components/ui'

const { t } = useI18n()
</script>

<template>
  <div class="voice-admin">
    <header class="voice-admin__header">
      <div>
        <h1>{{ t('admin.page.voice.title') }}</h1>
        <p class="voice-admin__subtitle">
          {{ t('admin.page.voice.subtitle') }}
        </p>
      </div>
      <div class="admin-header-badges">
        <UiBadge variant="warning">{{ t('admin.page.deploymentBoundBadge') }}</UiBadge>
        <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
      </div>
    </header>

    <AdminCharacterPicker
      :hint="t('admin.page.voice.pickerHint')"
    >
      <template #default="{ character, patch }">
        <UiCard size="lg">
          <template #header>
            <h2 class="voice-admin__card-title">{{ t('admin.page.voice.cardTitle', { name: character.name }) }}</h2>
          </template>

          <VoiceProfilePanel :character="character" @updated="patch" />
        </UiCard>
      </template>
    </AdminCharacterPicker>
  </div>
</template>

<style scoped>
.voice-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.voice-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.voice-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.voice-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.voice-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
}
</style>
