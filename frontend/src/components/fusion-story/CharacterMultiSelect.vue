<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiBadge } from '@/components/ui'
import type { Character } from '@/types/character'

type MaterialTier = 'rich' | 'ok' | 'sparse'

const props = defineProps<{
  characters: Character[]
  modelValue: string[]
  min?: number
  max?: number
  // Per-character fusion material richness (Creator Studio C1-P1). When
  // absent (e.g. branching drama, which is persona-only) no badge renders
  // and the component behaves exactly as before.
  materialStats?: Record<string, { tier: MaterialTier }>
}>()

const BADGE_VARIANT: Record<MaterialTier, 'success' | 'default' | 'warning'> = {
  rich: 'success',
  ok: 'default',
  sparse: 'warning',
}

function tierOf(id: string): MaterialTier | null {
  return props.materialStats?.[id]?.tier ?? null
}

const emit = defineEmits<{
  (e: 'update:modelValue', value: string[]): void
}>()

const { t } = useI18n()

const min = computed(() => props.min ?? 2)
const max = computed(() => props.max ?? 5)

const selected = computed({
  get: () => props.modelValue,
  set: (next: string[]) => emit('update:modelValue', next),
})

function toggle(id: string) {
  const next = new Set(selected.value)
  if (next.has(id)) {
    next.delete(id)
  } else {
    if (next.size >= max.value) return
    next.add(id)
  }
  selected.value = Array.from(next)
}

function isChecked(id: string): boolean {
  return selected.value.includes(id)
}

function isDisabled(id: string): boolean {
  return !isChecked(id) && selected.value.length >= max.value
}

const helperText = computed(() => {
  if (selected.value.length < min.value) {
    return t('fusionStory.characterSelect.needMore', {
      min: min.value,
      selected: selected.value.length,
    })
  }
  if (selected.value.length >= max.value) {
    return t('fusionStory.characterSelect.maxReached', { max: max.value })
  }
  return t('fusionStory.characterSelect.selected', {
    selected: selected.value.length,
    max: max.value,
  })
})
</script>

<template>
  <div class="multi-select">
    <div class="multi-select__hint">{{ helperText }}</div>
    <div class="multi-select__grid">
      <label
        v-for="char in characters"
        :key="char.id"
        class="multi-select__item"
        :class="{
          'is-checked': isChecked(char.id),
          'is-disabled': isDisabled(char.id),
        }"
      >
        <input
          type="checkbox"
          :checked="isChecked(char.id)"
          :disabled="isDisabled(char.id)"
          @change="toggle(char.id)"
        />
        <div class="multi-select__avatar">
          <img v-if="char.image_urls?.[0]" :src="char.image_urls[0]" :alt="char.name" />
          <span v-else>{{ char.name?.[0] ?? '?' }}</span>
        </div>
        <div class="multi-select__meta">
          <div class="multi-select__name-row">
            <span class="multi-select__name">{{ char.name }}</span>
            <UiBadge
              v-if="tierOf(char.id)"
              :variant="BADGE_VARIANT[tierOf(char.id)!]"
              class="multi-select__badge"
            >
              {{ t(`fusionStory.characterSelect.material.${tierOf(char.id)}`) }}
            </UiBadge>
          </div>
          <div class="multi-select__summary">
            {{ char.summary || t('fusionStory.characterSelect.noSummary') }}
          </div>
        </div>
      </label>
    </div>
  </div>
</template>

<style scoped>
.multi-select {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.multi-select__hint {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.7);
}
.multi-select__grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
}
.multi-select__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.055), rgba(255, 255, 255, 0.025)),
    rgba(18, 12, 42, 0.42);
  cursor: pointer;
  transition: border-color 140ms ease, transform 140ms ease, box-shadow 140ms ease;
}
.multi-select__item:hover {
  border-color: rgba(var(--color-primary-rgb), 0.54);
}
.multi-select__item.is-checked {
  border-color: rgba(var(--color-spark-rgb), 0.78);
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.18), rgba(var(--color-spark-rgb), 0.06)),
    rgba(18, 12, 42, 0.54);
  box-shadow:
    0 0 0 1px rgba(var(--color-spark-rgb), 0.18) inset,
    0 0 20px rgba(var(--color-primary-rgb), 0.2);
}
.multi-select__item.is-disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.multi-select__avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.1);
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  flex-shrink: 0;
  transition: transform 140ms ease, box-shadow 140ms ease, border-color 140ms ease;
  border: 1px solid transparent;
}
.multi-select__item.is-checked .multi-select__avatar {
  transform: scale(1.12);
  border-color: rgba(var(--color-spark-rgb), 0.82);
  box-shadow:
    0 0 0 2px rgba(var(--color-primary-rgb), 0.22),
    0 0 20px rgba(var(--color-spark-rgb), 0.2);
}
.multi-select__avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.multi-select__meta {
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.multi-select__name-row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.multi-select__name {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}
.multi-select__badge {
  flex-shrink: 0;
  font-size: 10px;
}
.multi-select__summary {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.6);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 768px) {
  .multi-select__grid {
    /* minmax(0, 1fr)（而非 1fr）：1fr 的隱含下限是內容 min-content，
       角色簡介是 nowrap 單行，會把整欄撐到超出手機視口。 */
    grid-template-columns: minmax(0, 1fr);
    gap: 6px;
  }
  .multi-select__item {
    padding: 8px;
  }
  .multi-select__avatar {
    width: 32px;
    height: 32px;
  }
  .multi-select__name {
    font-size: 13px;
  }
  .multi-select__summary {
    font-size: 11px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .multi-select__item,
  .multi-select__avatar,
  .multi-select__item.is-checked .multi-select__avatar {
    transform: none;
    transition: none;
  }
}
</style>
