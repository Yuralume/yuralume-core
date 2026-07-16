<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import ImageProfilesPicker from '@/components/ImageProfilesPicker.vue'
import AdminScopeSelector from '@/components/admin/AdminScopeSelector.vue'
import { UiCard, UiBadge } from '@/components/ui'

const { t } = useI18n()
const characterId = ref<string | undefined>(undefined)
</script>

<template>
  <div class="image-profiles-admin">
    <header class="image-profiles-admin__header">
      <div>
        <h1>{{ t('admin.page.imageProfiles.title') }}</h1>
        <p class="image-profiles-admin__subtitle">
          {{ t('admin.page.imageProfiles.subtitle') }}
        </p>
      </div>
      <div class="admin-header-badges">
        <UiBadge variant="warning">{{ t('admin.page.deploymentBoundBadge') }}</UiBadge>
        <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
      </div>
    </header>

    <AdminScopeSelector
      v-model="characterId"
      :hint="t('admin.page.imageProfiles.scopeHint')"
    />

    <UiCard size="lg">
      <template #header>
        <h2 class="image-profiles-admin__card-title">
          {{ characterId ? t('admin.page.imageProfiles.cardTitleCharacter') : t('admin.page.imageProfiles.cardTitleGlobal') }}
        </h2>
      </template>

      <ImageProfilesPicker :character-id="characterId" />
    </UiCard>
  </div>
</template>

<style scoped>
.image-profiles-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.image-profiles-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.image-profiles-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.image-profiles-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.image-profiles-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
</style>
