<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import MemoryBrowserPanel from '@/components/MemoryBrowserPanel.vue'
import OperatorPersonaPanel from '@/components/OperatorPersonaPanel.vue'
import DispositionDriftHistoryPanel from '@/components/admin/DispositionDriftHistoryPanel.vue'
import AdminCharacterPicker from '@/components/admin/AdminCharacterPicker.vue'
import { UiCard, UiBadge } from '@/components/ui'

const { t } = useI18n()

type Tab = 'memory' | 'persona' | 'drift'
const activeTab = ref<Tab>('memory')
</script>

<template>
  <div class="memories-admin">
    <header class="memories-admin__header">
      <div>
        <h1>{{ t('admin.page.memories.title') }}</h1>
        <p class="memories-admin__subtitle">
          {{ t('admin.page.memories.subtitle') }}
        </p>
      </div>
      <UiBadge variant="primary">{{ t('admin.page.phase3Badge') }}</UiBadge>
    </header>

    <AdminCharacterPicker
      :hint="t('admin.page.memories.pickerHint')"
    >
      <template #default="{ character }">
        <nav class="memories-admin__tabs" role="tablist">
          <button
            type="button"
            role="tab"
            :class="['memories-admin__tab', { active: activeTab === 'memory' }]"
            :aria-selected="activeTab === 'memory'"
            @click="activeTab = 'memory'"
          >{{ t('admin.page.memories.tabs.memory') }}</button>
          <button
            type="button"
            role="tab"
            :class="['memories-admin__tab', { active: activeTab === 'persona' }]"
            :aria-selected="activeTab === 'persona'"
            @click="activeTab = 'persona'"
          >{{ t('admin.page.memories.tabs.persona') }}</button>
          <button
            type="button"
            role="tab"
            :class="['memories-admin__tab', { active: activeTab === 'drift' }]"
            :aria-selected="activeTab === 'drift'"
            @click="activeTab = 'drift'"
          >{{ t('admin.page.memories.tabs.drift') }}</button>
        </nav>

        <UiCard v-if="activeTab === 'memory'" size="lg">
          <template #header>
            <h2 class="memories-admin__card-title">{{ t('admin.page.memories.cardTitleMemory', { name: character.name }) }}</h2>
          </template>
          <MemoryBrowserPanel :key="`mem-${character.id}`" :character-id="character.id" />
        </UiCard>

        <UiCard v-else-if="activeTab === 'persona'" size="lg">
          <template #header>
            <h2 class="memories-admin__card-title">
              {{ t('admin.page.memories.cardTitlePersona', { name: character.name }) }}
            </h2>
          </template>
          <OperatorPersonaPanel
            :key="`persona-${character.id}`"
            :character-id="character.id"
          />
        </UiCard>

        <UiCard v-else size="lg">
          <template #header>
            <h2 class="memories-admin__card-title">
              {{ t('admin.page.memories.cardTitleDrift', { name: character.name }) }}
            </h2>
          </template>
          <DispositionDriftHistoryPanel
            :key="`drift-${character.id}`"
            :character-id="character.id"
          />
        </UiCard>
      </template>
    </AdminCharacterPicker>
  </div>
</template>

<style scoped>
.memories-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.memories-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.memories-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.memories-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.memories-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.memories-admin__tabs {
  display: flex;
  gap: var(--space-1);
  border-bottom: 1px solid var(--color-border);
}
.memories-admin__tab {
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  padding: var(--space-2) var(--space-3);
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  font-weight: 500;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
}
.memories-admin__tab:hover {
  color: var(--color-text);
}
.memories-admin__tab.active {
  color: var(--color-text);
  border-bottom-color: var(--color-primary, #5b9dff);
}
</style>
