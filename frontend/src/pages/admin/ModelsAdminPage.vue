<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { listProviders } from '@/utils/api/system'
import FeatureModelsPicker from '@/components/FeatureModelsPicker.vue'
import NsfwModeTargetSetting from '@/components/NsfwModeTargetSetting.vue'
import AdminScopeSelector from '@/components/admin/AdminScopeSelector.vue'
import { UiCard, UiBadge } from '@/components/ui'

const { t } = useI18n()

// 全域 / per-character 切換在 AdminScopeSelector，picker 真正吃的 prop
// 是 characterId（undefined = 全域）。providers 清單 admin 自己抓，避免
// 跨頁面共用 store。
const providers = ref<string[]>([])
const characterId = ref<string | undefined>(undefined)
const providersLoaded = ref(false)

onMounted(async () => {
  try {
    providers.value = await listProviders()
  } catch (err) {
    notification.error({
      message: t('admin.page.models.errors.loadProvidersFailed'),
      description: err instanceof Error ? err.message : String(err),
      duration: 4,
    })
  } finally {
    providersLoaded.value = true
  }
})
</script>

<template>
  <div class="models-admin">
    <header class="models-admin__header">
      <div>
        <h1>{{ t('admin.page.models.title') }}</h1>
        <p class="models-admin__subtitle">
          {{ t('admin.page.models.subtitle') }}
        </p>
      </div>
      <div class="admin-header-badges">
        <UiBadge variant="warning">{{ t('admin.page.deploymentBoundBadge') }}</UiBadge>
        <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
      </div>
    </header>

    <UiCard size="lg">
      <template #header>
        <h2 class="models-admin__card-title">
          {{ t('nsfwModeTargetSetting.title') }}
        </h2>
      </template>

      <NsfwModeTargetSetting
        :providers="providers"
        :providers-loaded="providersLoaded"
      />
    </UiCard>

    <AdminScopeSelector
      v-model="characterId"
      :hint="t('admin.page.models.scopeHint')"
    />

    <UiCard size="lg">
      <template #header>
        <h2 class="models-admin__card-title">
          {{ characterId ? t('admin.page.models.cardTitleCharacter') : t('admin.page.models.cardTitleGlobal') }}
        </h2>
      </template>

      <div v-if="!providersLoaded" class="models-admin__hint">{{ t('admin.page.models.loadingProviders') }}</div>

      <div v-else-if="providers.length === 0" class="models-admin__hint">
        {{ t('admin.page.models.emptyProviders') }}
      </div>

      <FeatureModelsPicker
        v-else
        :providers="providers"
        :character-id="characterId"
      />
    </UiCard>
  </div>
</template>

<style scoped>
.models-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.models-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.models-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.models-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.models-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.models-admin__hint {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  padding: var(--space-2) 0;
}
</style>
