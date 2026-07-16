<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  deleteMemory,
  listMemories,
  searchMemories,
  updateMemory,
  type Memory,
  type MemoryScored,
} from '@/utils/api/memories'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { formatDateTime } from '@/i18n/formatters'
import { UiButton } from '@/components/ui'

const props = defineProps<{
  characterId: string | null
}>()
const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()

type KindFilter = 'all' | 'semantic' | 'relationship' | 'episodic' | 'reflection'

const KIND_LABEL_KEY: Record<string, string> = {
  semantic: 'memoryBrowser.kind.semantic',
  relationship: 'memoryBrowser.kind.relationship',
  episodic: 'memoryBrowser.kind.episodic',
  reflection: 'memoryBrowser.kind.reflection',
}

const FILTER_KINDS: KindFilter[] = ['all', 'semantic', 'relationship', 'episodic', 'reflection']

const memories = ref<Memory[]>([])
const loading = ref(false)
const errorMsg = ref<string | null>(null)
const kindFilter = ref<KindFilter>('all')

const editingId = ref<string | null>(null)
const editContent = ref('')
const editSalience = ref(0.5)
const editTags = ref('')
const editBusy = ref(false)

const searchQuery = ref('')
const searchResults = ref<MemoryScored[] | null>(null)
const searchBusy = ref(false)

async function reload() {
  if (!props.characterId) {
    memories.value = []
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    memories.value = await listMemories(props.characterId, {
      kind: kindFilter.value === 'all' ? undefined : kindFilter.value,
    })
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('memoryBrowser.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

function startEdit(item: Memory) {
  editingId.value = item.id
  editContent.value = item.content
  editSalience.value = item.salience
  editTags.value = item.tags.join(', ')
}

function cancelEdit() {
  editingId.value = null
  editContent.value = ''
  editSalience.value = 0.5
  editTags.value = ''
}

async function saveEdit() {
  if (!editingId.value) return
  const content = editContent.value.trim()
  if (!content) {
    errorMsg.value = t('memoryBrowser.errors.contentRequired')
    return
  }
  editBusy.value = true
  errorMsg.value = null
  try {
    const tags = editTags.value
      .split(',')
      .map(t => t.trim())
      .filter(Boolean)
    const updated = await updateMemory(editingId.value, {
      content,
      salience: editSalience.value,
      tags,
    })
    memories.value = memories.value.map(m => m.id === updated.id ? updated : m)
    cancelEdit()
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('memoryBrowser.errors.updateFailed')
  } finally {
    editBusy.value = false
  }
}

async function handleDelete(item: Memory) {
  if (!await confirmDialog({
    content: t('memoryBrowser.confirmDelete', { content: item.content.slice(0, 80) }),
    okText: t('common.actions.delete'),
    danger: true,
  })) {
    return
  }
  try {
    await deleteMemory(item.id)
    memories.value = memories.value.filter(m => m.id !== item.id)
    if (searchResults.value) {
      searchResults.value = searchResults.value.filter(s => s.item.id !== item.id)
    }
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('memoryBrowser.errors.deleteFailed')
  }
}

async function runSearch() {
  if (!props.characterId) return
  const q = searchQuery.value.trim()
  if (!q) {
    searchResults.value = null
    return
  }
  searchBusy.value = true
  errorMsg.value = null
  try {
    searchResults.value = await searchMemories(props.characterId, q, 8)
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('memoryBrowser.errors.searchFailed')
  } finally {
    searchBusy.value = false
  }
}

function clearSearch() {
  searchQuery.value = ''
  searchResults.value = null
}

function formatTime(raw: string): string {
  return formatDateTime(raw, locale.value, timeZone.value)
}

function kindLabel(kind: string): string {
  const key = KIND_LABEL_KEY[kind]
  return key ? t(key) : kind
}

function filterLabel(kind: KindFilter): string {
  return kind === 'all' ? t('memoryBrowser.kind.all') : kindLabel(kind)
}

watch(() => props.characterId, () => {
  cancelEdit()
  clearSearch()
  reload()
}, { immediate: true })

watch(kindFilter, () => reload())
</script>

<template>
  <div class="memory-panel">
    <div v-if="!characterId" class="memory-empty">{{ t('memoryBrowser.noCharacter') }}</div>

    <template v-else>
      <div class="memory-header">
        <h3 class="section-title">{{ t('memoryBrowser.title') }}</h3>
        <p class="memory-hint">
          {{ t('memoryBrowser.hint') }}
        </p>
      </div>

      <!-- 試查（hybrid ranker preview） -->
      <div class="memory-search">
        <input
          v-model="searchQuery"
          type="text"
          class="field-input"
          :placeholder="t('memoryBrowser.searchPlaceholder')"
          @keydown.enter="runSearch"
        />
        <div class="memory-search-actions">
          <UiButton
            size="sm"
            :loading="searchBusy"
            :disabled="!searchQuery.trim()"
            @click="runSearch"
          >{{ searchBusy ? t('memoryBrowser.searching') : t('memoryBrowser.searchAction') }}</UiButton>
          <UiButton
            v-if="searchResults"
            size="sm"
            @click="clearSearch"
          >{{ t('common.actions.clear') }}</UiButton>
        </div>
      </div>

      <!-- 搜尋結果 -->
      <div v-if="searchResults" class="memory-search-results">
        <div class="memory-result-title">{{ t('memoryBrowser.searchResultTitle', { count: searchResults.length }) }}</div>
        <div v-if="searchResults.length === 0" class="memory-empty-small">
          {{ t('memoryBrowser.noSearchResults') }}
        </div>
        <div
          v-for="scored in searchResults"
          :key="scored.item.id"
          class="memory-card memory-card-scored"
        >
          <div class="memory-card-head">
            <span :class="['kind-badge', `kind-${scored.item.kind}`]">
              {{ kindLabel(scored.item.kind) }}
            </span>
            <span class="similarity-badge">
              {{ t('memoryBrowser.similarityPercent', { percent: (scored.similarity * 100).toFixed(0) }) }}
            </span>
            <span class="salience-badge">
              {{ t('memoryBrowser.saliencePercent', { percent: (scored.item.salience * 100).toFixed(0) }) }}
            </span>
          </div>
          <div class="memory-content">{{ scored.item.content }}</div>
        </div>
      </div>

      <!-- Kind 過濾 -->
      <div class="memory-filter">
        <label class="field-label">{{ t('memoryBrowser.filterLabel') }}</label>
        <div class="kind-tabs">
          <button
            v-for="k in FILTER_KINDS"
            :key="k"
            :class="['kind-tab', { active: kindFilter === k }]"
            @click="kindFilter = k"
          >{{ filterLabel(k) }}</button>
        </div>
      </div>

      <!-- 錯誤 -->
      <div v-if="errorMsg" class="memory-error">{{ errorMsg }}</div>

      <!-- 列表 -->
      <div v-if="loading" class="memory-empty">{{ t('common.state.loading') }}</div>
      <div v-else-if="memories.length === 0" class="memory-empty">
        {{ t('memoryBrowser.empty') }}
      </div>
      <div v-else class="memory-list">
        <div
          v-for="item in memories"
          :key="item.id"
          class="memory-card"
        >
          <!-- 編輯中 -->
          <template v-if="editingId === item.id">
            <textarea
              v-model="editContent"
              class="field-textarea"
              rows="3"
            />
            <div class="edit-row">
              <label class="field-label edit-label">{{ t('memoryBrowser.salienceLabel') }}</label>
              <input
                v-model.number="editSalience"
                type="range"
                min="0"
                max="1"
                step="0.05"
                class="field-range"
              />
              <span class="range-value">{{ (editSalience * 100).toFixed(0) }}%</span>
            </div>
            <input
              v-model="editTags"
              type="text"
              class="field-input"
              :placeholder="t('memoryBrowser.tagsPlaceholder')"
            />
            <div class="edit-actions">
              <UiButton
                variant="primary"
                size="sm"
                :loading="editBusy"
                @click="saveEdit"
              >{{ editBusy ? t('common.state.saving') : t('common.actions.save') }}</UiButton>
              <UiButton
                size="sm"
                :disabled="editBusy"
                @click="cancelEdit"
              >{{ t('common.actions.cancel') }}</UiButton>
            </div>
          </template>

          <!-- 顯示模式 -->
          <template v-else>
            <div class="memory-card-head">
              <span :class="['kind-badge', `kind-${item.kind}`]">
                {{ kindLabel(item.kind) }}
              </span>
              <span class="salience-badge">
                {{ t('memoryBrowser.saliencePercent', { percent: (item.salience * 100).toFixed(0) }) }}
              </span>
              <span v-if="!item.has_embedding" class="no-embed-badge" :title="t('memoryBrowser.noEmbeddingTitle')">
                {{ t('memoryBrowser.noEmbeddingBadge') }}
              </span>
            </div>
            <div class="memory-content">{{ item.content }}</div>
            <div v-if="item.tags.length" class="memory-tags">
              <span v-for="t in item.tags" :key="t" class="tag-chip">#{{ t }}</span>
            </div>
            <div class="memory-meta">
              <span>{{ t('memoryBrowser.createdAt', { time: formatTime(item.created_at) }) }}</span>
              <span v-if="item.access_count">{{ t('memoryBrowser.recallCount', { count: item.access_count }) }}</span>
            </div>
            <div class="memory-actions">
              <button class="btn-icon" :title="t('common.actions.edit')" @click="startEdit(item)">✎</button>
              <button class="btn-icon btn-icon-danger" :title="t('common.actions.delete')" @click="handleDelete(item)">×</button>
            </div>
          </template>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.memory-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.memory-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 0;
}

.memory-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin: 0;
}

.memory-empty {
  padding: 18px 12px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--color-border);
  border-radius: 8px;
}

.memory-empty-small {
  padding: 10px;
  font-size: 11px;
  color: var(--color-text-secondary);
  text-align: center;
}

.memory-error {
  padding: 8px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}

/* --- search ---- */
.memory-search {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: rgba(107, 153, 178, 0.08);
  border: 1px solid rgba(107, 153, 178, 0.25);
  border-radius: 6px;
}

.memory-search-actions {
  display: flex;
  gap: 6px;
}

.memory-search-results {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: rgba(255, 255, 255, 0.02);
  border-radius: 6px;
}

.memory-result-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  letter-spacing: 0.5px;
}

.memory-card-scored {
  background: rgba(107, 153, 178, 0.06);
}

/* --- filter --- */
.memory-filter {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.kind-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.kind-tab {
  flex: 1 0 auto;
  padding: 4px 10px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  color: var(--color-text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: background 0.2s, color 0.2s;
}

.kind-tab.active {
  background: rgba(183, 93, 63, 0.2);
  color: var(--color-primary-light);
  border-color: var(--color-primary);
}

/* --- list --- */
.memory-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.memory-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}

.memory-card-head {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  align-items: center;
}

.kind-badge, .salience-badge, .similarity-badge, .no-embed-badge {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
}

.kind-badge {
  font-weight: 600;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text);
}

.kind-semantic { background: rgba(107, 153, 178, 0.2); color: #8cb4cc; }
.kind-relationship { background: rgba(183, 93, 63, 0.2); color: #d89680; }
.kind-episodic { background: rgba(72, 159, 115, 0.2); color: #7dc49a; }
.kind-reflection { background: rgba(230, 162, 60, 0.2); color: #e6a23c; }

.salience-badge {
  background: rgba(183, 93, 63, 0.12);
  color: #d89680;
}

.similarity-badge {
  background: rgba(107, 153, 178, 0.18);
  color: #8cb4cc;
}

.no-embed-badge {
  background: rgba(231, 76, 60, 0.12);
  color: #ff8a75;
}

.memory-content {
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
  word-break: break-word;
  white-space: pre-wrap;
}

.memory-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.tag-chip {
  font-size: 10px;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
}

.memory-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 10px;
  color: var(--color-text-secondary);
}

.memory-actions {
  position: absolute;
  top: 6px;
  right: 6px;
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.2s;
}

.memory-card:hover .memory-actions {
  opacity: 1;
}

@media (hover: none) {
  .memory-actions { opacity: 0.7; }
}

.btn-icon {
  width: 24px;
  height: 24px;
  border-radius: 4px;
  border: none;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.btn-icon:hover {
  background: rgba(255, 255, 255, 0.16);
  color: var(--color-text);
}

.btn-icon-danger:hover {
  background: rgba(231, 76, 60, 0.25);
  color: #ff8a75;
}

/* --- edit --- */
.edit-row {
  display: grid;
  grid-template-columns: 60px 1fr 40px;
  align-items: center;
  gap: 6px;
}

.edit-label {
  margin: 0;
}

.edit-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}

/* 共用欄位樣式在 global style.css；此處縮小 padding / 字級以配合 compact 列表 */
.field-input, .field-textarea {
  padding: 6px 10px;
  font-size: 12px;
}

.range-value {
  font-size: 11px;
  color: var(--color-text-secondary);
  text-align: right;
}

/* .field-label 在 global style.css */

</style>
