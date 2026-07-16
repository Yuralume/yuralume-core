<script setup lang="ts">
import { onMounted, ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import type { Character } from '@/types/character'
import { listCharacters } from '@/utils/api/characters'
import { UiCard, UiSelect } from '@/components/ui'

/**
 * Shared scope selector for admin pages that wrap a picker supporting both
 * global mode and per-character override mode (FeatureModelsPicker /
 * ImageProfilesPicker / VideoProfilesPicker). Reads / writes ``?scope=``
 * URL query for shareable deep links — ``scope=__global__`` (default) or
 * ``scope=<character-id>``.
 *
 * Parent reads the resolved ``characterId`` via v-model (undefined = global).
 */
const props = defineProps<{
  modelValue: string | undefined
  title?: string
  hint?: string
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: string | undefined): void
}>()

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const characters = ref<Character[]>([])
const loadError = ref<string | null>(null)
const loaded = ref(false)

const GLOBAL = '__global__'

const selectValue = computed<string>({
  get: () => props.modelValue ?? GLOBAL,
  set: (v) => {
    const next = v === GLOBAL ? undefined : v
    emit('update:modelValue', next)
    router.replace({
      query: { ...route.query, scope: next ?? undefined },
    })
  },
})

const options = computed(() => [
  { value: GLOBAL, label: t('admin.selector.globalOption') },
  ...characters.value.map(c => ({ value: c.id, label: t('admin.selector.characterOverride', { name: c.name }) })),
])

onMounted(async () => {
  try {
    characters.value = await listCharacters()
    const qs = typeof route.query.scope === 'string' ? route.query.scope : ''
    if (qs && qs !== GLOBAL && characters.value.some(c => c.id === qs)) {
      emit('update:modelValue', qs)
    }
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : t('admin.selector.errors.loadCharactersFailed')
  } finally {
    loaded.value = true
  }
})

// Defensive: if parent passes a characterId that no longer matches any
// known character (e.g. character was deleted in another tab), fall back
// to global so the picker doesn't keep posting to a dead row.
watch([characters, () => props.modelValue], ([cs, mv]) => {
  if (mv && cs.length > 0 && !cs.some(c => c.id === mv)) {
    emit('update:modelValue', undefined)
  }
})
</script>

<template>
    <UiCard>
      <template #header>
      <h2 class="scope-card__title">{{ title ?? t('admin.selector.scopeTitle') }}</h2>
    </template>

    <p v-if="hint" class="scope-card__hint">{{ hint }}</p>

    <div v-if="loadError" class="scope-card__error">
      {{ t('admin.selector.errors.loadCharactersFailedWithReason', { reason: loadError }) }}
    </div>

    <UiSelect
      v-else
      v-model="selectValue"
      :label="t('admin.selector.scopeLabel')"
      :hint="t('admin.selector.scopeHint')"
      :options="options"
      :disabled="!loaded"
    />
  </UiCard>
</template>

<style scoped>
.scope-card__title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.scope-card__hint {
  margin: 0 0 var(--space-2);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.scope-card__error {
  color: #f4a3a3;
  font-size: var(--font-sm);
}
</style>
