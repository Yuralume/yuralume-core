<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import type { Character } from '@/types/character'
import { listCharacters } from '@/utils/api/characters'
import { UiCard, UiSelect } from '@/components/ui'

/**
 * Required-character picker for admin pages whose underlying panel is
 * inherently per-character (VoiceProfilePanel / CharacterLorasPanel /
 * CharacterRelationshipsPanel ...). Differs from AdminScopeSelector —
 * there is no "global" mode here.
 *
 * Reads / writes ``?character=`` URL query for shareable deep links.
 * Exposes the resolved Character via scoped slot, plus a ``patch``
 * callback so panel ``@updated`` events keep the internal list in sync
 * without forcing the parent to refetch.
 */
defineProps<{
  title?: string
  hint?: string
}>()

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const characters = ref<Character[]>([])
const selectedId = ref<string>('')
const loadError = ref<string | null>(null)
const loaded = ref(false)

const selectedCharacter = computed(
  () => characters.value.find(c => c.id === selectedId.value) ?? null,
)

async function refresh() {
  try {
    characters.value = await listCharacters()
    if (!characters.value.some(c => c.id === selectedId.value)) {
      selectedId.value = characters.value[0]?.id ?? ''
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : t('admin.selector.errors.loadCharactersFailed')
  } finally {
    loaded.value = true
  }
}

function patch(updated: Character) {
  const idx = characters.value.findIndex(c => c.id === updated.id)
  if (idx >= 0) {
    characters.value[idx] = updated
  }
}

onMounted(async () => {
  const qs = typeof route.query.character === 'string' ? route.query.character : ''
  if (qs) selectedId.value = qs
  await refresh()
})

watch(selectedId, (next) => {
  router.replace({ query: { ...route.query, character: next || undefined } })
})

defineExpose({ refresh, patch })
</script>

<template>
  <div class="admin-character-picker">
    <UiCard>
      <template #header>
        <h2 class="picker__title">{{ title ?? t('admin.selector.characterTitle') }}</h2>
      </template>

      <p v-if="hint" class="picker__hint">{{ hint }}</p>

      <div v-if="loadError" class="picker__error">
        {{ t('admin.selector.errors.loadCharactersFailedWithReason', { reason: loadError }) }}
      </div>

      <div v-else-if="loaded && characters.length === 0" class="picker__empty">
        {{ t('admin.selector.noCharacters') }}
      </div>

      <UiSelect
        v-else
        v-model="selectedId"
        :label="t('admin.selector.characterLabel')"
        :options="characters.map(c => ({ value: c.id, label: c.name }))"
        :disabled="!loaded || characters.length === 0"
      />
    </UiCard>

    <slot
      v-if="selectedCharacter"
      :character="selectedCharacter"
      :patch="patch"
    />
  </div>
</template>

<style scoped>
.admin-character-picker {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.picker__title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.picker__hint {
  margin: 0 0 var(--space-2);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.picker__error {
  color: #f4a3a3;
  font-size: var(--font-sm);
}
.picker__empty {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
</style>
