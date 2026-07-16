<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import VideoProfilesPicker from '@/components/VideoProfilesPicker.vue'
import AdminScopeSelector from '@/components/admin/AdminScopeSelector.vue'
import { UiCard, UiBadge } from '@/components/ui'

const { t } = useI18n()
const characterId = ref<string | undefined>(undefined)
</script>

<template>
  <div class="video-profiles-admin">
    <header class="video-profiles-admin__header">
      <div>
        <h1>{{ t('admin.page.videoProfiles.title') }}</h1>
        <p class="video-profiles-admin__subtitle">
          {{ t('admin.page.videoProfiles.subtitle') }}
        </p>
      </div>
      <div class="admin-header-badges">
        <UiBadge variant="warning">{{ t('admin.page.deploymentBoundBadge') }}</UiBadge>
        <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
      </div>
    </header>

    <AdminScopeSelector
      v-model="characterId"
      :hint="t('admin.page.videoProfiles.scopeHint')"
    />

    <UiCard size="lg">
      <template #header>
        <h2 class="video-profiles-admin__card-title">
          {{ characterId ? t('admin.page.videoProfiles.cardTitleCharacter') : t('admin.page.videoProfiles.cardTitleGlobal') }}
        </h2>
      </template>

      <VideoProfilesPicker :character-id="characterId" />
    </UiCard>
  </div>
</template>

<style scoped>
.video-profiles-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.video-profiles-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.video-profiles-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.video-profiles-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.video-profiles-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
</style>
