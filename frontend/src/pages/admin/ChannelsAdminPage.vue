<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import MessagingPublicBaseUrlSettings from '@/components/MessagingPublicBaseUrlSettings.vue'

const { t } = useI18n()
</script>

<template>
  <div class="admin-channels-page">
    <header class="admin-channels-page__header">
      <h1>{{ t('admin.page.channels.title') }}</h1>
      <p>{{ t('admin.page.channels.subtitle') }}</p>
    </header>

    <MessagingPublicBaseUrlSettings />
  </div>
</template>

<style scoped>
.admin-channels-page {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  max-width: 1100px;
}

.admin-channels-page__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}

.admin-channels-page__header p {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
</style>
