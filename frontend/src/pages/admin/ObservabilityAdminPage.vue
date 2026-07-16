<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import { listCharacters } from '@/utils/api/characters'
import ObservabilityPanel from '@/components/observability/ObservabilityPanel.vue'
import { UiCard, UiSelect, UiBadge } from '@/components/ui'

const { t } = useI18n()

// 觀測頁需要綁定一個 character context 才能拉出 funnel / emotion 資料。
// 進站若 URL 帶 ?character= 就沿用；否則挑第一個角色 (若有)。null = 全域觀測。
const route = useRoute()
const router = useRouter()

const characters = ref<Character[]>([])
const selected = ref<string>('')
const loadError = ref<string | null>(null)

onMounted(async () => {
  try {
    characters.value = await listCharacters()
    const qsChar = typeof route.query.character === 'string' ? route.query.character : ''
    if (qsChar && characters.value.some(c => c.id === qsChar)) {
      selected.value = qsChar
    } else if (characters.value.length > 0) {
      selected.value = characters.value[0].id
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : t('common.errors.loadFailed', { reason: t('common.errors.unknown') })
  }
})

watch(selected, (next) => {
  // 把選擇寫回 URL，方便分享或重新整理保留 context。
  router.replace({ query: { ...route.query, character: next || undefined } })
})
</script>

<template>
  <div class="observability-admin">
    <header class="observability-admin__header">
      <div>
        <h1>{{ t('admin.page.observability.title') }}</h1>
        <p class="observability-admin__subtitle">
          {{ t('admin.page.observability.subtitle') }}
        </p>
      </div>
      <UiBadge variant="primary">{{ t('admin.page.observability.badge') }}</UiBadge>
    </header>

    <UiCard size="lg">
      <template #header>
        <h2 class="observability-admin__card-title">{{ t('admin.page.observability.contextTitle') }}</h2>
      </template>

      <div v-if="loadError" class="observability-admin__error">
        {{ t('admin.selector.errors.loadCharactersFailedWithReason', { reason: loadError }) }}
      </div>

      <div v-else-if="characters.length === 0" class="observability-admin__hint">
        {{ t('admin.page.observability.noCharacters') }}
      </div>

      <UiSelect
        v-else
        v-model="selected"
        :label="t('admin.page.observability.targetLabel')"
        :hint="t('admin.page.observability.targetHint')"
        :options="characters.map(c => ({ value: c.id, label: c.name }))"
      />
    </UiCard>

    <ObservabilityPanel
      v-if="selected"
      :character-id="selected"
      :characters="characters.map(c => ({ id: c.id, name: c.name }))"
    />
  </div>
</template>

<style scoped>
.observability-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.observability-admin__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.observability-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.observability-admin__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.observability-admin__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.observability-admin__error {
  color: #f4a3a3;
  font-size: var(--font-sm);
}
.observability-admin__hint {
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
