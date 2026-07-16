<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'

const props = withDefaults(defineProps<{
  modelEnabled: boolean
  modelCategories: string[]
  modelExcluded: string[]
  copyNamespace?: string
  categoryNamespace?: string
}>(), {
  copyNamespace: 'interestSubscriptionPanel',
  categoryNamespace: '',
})

const emit = defineEmits<{
  (e: 'update:modelEnabled', v: boolean): void
  (e: 'update:modelCategories', v: string[]): void
  (e: 'update:modelExcluded', v: string[]): void
}>()

const { t } = useI18n()

const CANONICAL_CATEGORIES = [
  'news',
  'emergency',
  'weather',
  'tech',
  'gaming',
  'anime',
  'entertainment',
  'lifestyle',
  'food',
  'travel',
  'health',
  'science',
  'education',
  'sports',
  'culture',
  'finance',
] as const

type CanonicalCategory = (typeof CANONICAL_CATEGORIES)[number]

const excludedDraft = ref('')

const selectedCategories = computed(() => new Set(props.modelCategories))
const categoryRoot = computed(() => props.categoryNamespace || `${props.copyNamespace}.categories`)

function categoryLabel(id: CanonicalCategory): string {
  return t(`${categoryRoot.value}.${id}.label`)
}

function categoryHint(id: CanonicalCategory): string {
  return t(`${categoryRoot.value}.${id}.hint`)
}

function toggleEnabled(event: Event) {
  const el = event.target as HTMLInputElement
  emit('update:modelEnabled', el.checked)
}

function toggleCategory(id: string) {
  const next = new Set(props.modelCategories)
  if (next.has(id)) {
    next.delete(id)
  } else {
    next.add(id)
  }
  emit('update:modelCategories', Array.from(next))
}

function addExcluded() {
  const trimmed = excludedDraft.value.trim()
  if (!trimmed) return
  if (props.modelExcluded.includes(trimmed)) {
    excludedDraft.value = ''
    return
  }
  emit('update:modelExcluded', [...props.modelExcluded, trimmed])
  excludedDraft.value = ''
}

function removeExcluded(topic: string) {
  emit(
    'update:modelExcluded',
    props.modelExcluded.filter((t) => t !== topic),
  )
}

function onExcludedKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Enter') {
    ev.preventDefault()
    addExcluded()
  }
}
</script>

<template>
  <div class="interest-sub-panel">
    <p class="field-hint">
      {{ t(`${copyNamespace}.hint`) }}
    </p>

    <label class="field-label toggle-row">
      <input type="checkbox" :checked="modelEnabled" @change="toggleEnabled" />
      <span>{{ t(`${copyNamespace}.enabled`) }}</span>
    </label>

    <fieldset :disabled="!modelEnabled" class="sub-section">
      <legend class="field-label">{{ t(`${copyNamespace}.categoryLegend`) }}</legend>
      <div class="category-grid">
        <button
          v-for="cat in CANONICAL_CATEGORIES"
          :key="cat"
          type="button"
          class="cat-chip"
          :class="{ selected: selectedCategories.has(cat) }"
          :title="categoryHint(cat)"
          @click="toggleCategory(cat)"
        >
          {{ categoryLabel(cat) }}
        </button>
      </div>
    </fieldset>

    <fieldset :disabled="!modelEnabled" class="sub-section">
      <legend class="field-label">{{ t(`${copyNamespace}.excludedLegend`) }}</legend>
      <div class="excluded-input-row">
        <input
          v-model="excludedDraft"
          type="text"
          class="field-input"
          :placeholder="t(`${copyNamespace}.excludedPlaceholder`)"
          @keydown="onExcludedKeydown"
        />
        <button
          type="button"
          class="add-btn"
          :aria-label="t(`${copyNamespace}.addExcluded`)"
          @click="addExcluded"
        >＋</button>
      </div>
      <ul v-if="modelExcluded.length" class="excluded-list">
        <li v-for="topic in modelExcluded" :key="topic" class="excluded-chip">
          <span>{{ topic }}</span>
          <button
            type="button"
            class="remove-btn"
            :aria-label="t(`${copyNamespace}.removeExcluded`, { topic })"
            @click="removeExcluded(topic)"
          >×</button>
        </li>
      </ul>
    </fieldset>
  </div>
</template>

<style scoped>
.interest-sub-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.field-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-muted, #9aa0a6);
}
.toggle-row {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.sub-section {
  border: 0;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sub-section[disabled] {
  opacity: 0.5;
  pointer-events: none;
}
.category-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.cat-chip {
  background: rgba(255, 255, 255, 0.05);
  color: var(--color-text, #e8eaed);
  border: 1px solid rgba(255, 255, 255, 0.15);
  padding: 4px 12px;
  border-radius: 14px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}
.cat-chip:hover {
  border-color: rgba(255, 255, 255, 0.35);
}
.cat-chip.selected {
  background: linear-gradient(135deg, #6366f1, #a855f7);
  color: #fff;
  border-color: transparent;
}
.excluded-input-row {
  display: flex;
  gap: 6px;
}
.excluded-input-row .field-input {
  flex: 1;
}
.add-btn {
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text, #e8eaed);
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 8px;
  padding: 0 12px;
  font-size: 16px;
  cursor: pointer;
}
.add-btn:hover {
  background: rgba(255, 255, 255, 0.18);
}
.excluded-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.excluded-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: rgba(244, 67, 54, 0.18);
  color: #fca5a5;
  border: 1px solid rgba(244, 67, 54, 0.4);
  border-radius: 12px;
  padding: 2px 6px 2px 10px;
  font-size: 12px;
}
.remove-btn {
  background: transparent;
  border: 0;
  color: inherit;
  font-size: 14px;
  cursor: pointer;
  padding: 0 4px;
  line-height: 1;
}
</style>
