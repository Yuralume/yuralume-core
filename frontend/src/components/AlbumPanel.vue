<script setup lang="ts">
/**
 * 角色相簿面板。
 *
 * 顯示該角色所有相簿圖片（工具生成 + 從舞台轉來），支援：
 * - 點圖預覽（新分頁開啟原圖）
 * - 刪除（檔案一起刪）
 * - 晉升為舞台圖（加回 image_urls）
 *
 * 跟 CharacterImagesPanel 分開：此面板處理的是「長期收藏」，
 * 舞台面板處理的是「目前輪播中」。兩邊互相移動資料。
 */
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'

import type { AlbumItem } from '@/types/album'
import { UiButton } from '@/components/ui'
import {
  deleteAlbumItem,
  listAlbum,
  promoteAlbumToStage,
} from '@/utils/api/album'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { formatDateTime } from '@/i18n/formatters'

const props = defineProps<{
  characterId: string | null
}>()

const emit = defineEmits<{
  /** 晉升 / 舞台→相簿轉移後，通知上層刷新 character */
  characterUpdated: [characterId: string]
}>()

const { t, locale } = useI18n()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()

const items = ref<AlbumItem[]>([])
const loading = ref(false)
const busyItemId = ref<string | null>(null)
const errorMsg = ref<string | null>(null)

async function reload() {
  if (!props.characterId) {
    items.value = []
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    const res = await listAlbum(props.characterId)
    items.value = res.items
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('albumPanel.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

async function handleDelete(item: AlbumItem) {
  if (!await confirmDialog({
    content: t('albumPanel.confirm.delete'),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  busyItemId.value = item.id
  errorMsg.value = null
  try {
    await deleteAlbumItem(item.id)
    items.value = items.value.filter(i => i.id !== item.id)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('albumPanel.errors.deleteFailed')
  } finally {
    busyItemId.value = null
  }
}

async function handlePromote(item: AlbumItem) {
  if (!await confirmDialog({
    content: t('albumPanel.confirm.promote'),
  })) return
  busyItemId.value = item.id
  errorMsg.value = null
  try {
    await promoteAlbumToStage(item.id)
    items.value = items.value.filter(i => i.id !== item.id)
    emit('characterUpdated', item.character_id)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('albumPanel.errors.promoteFailed')
  } finally {
    busyItemId.value = null
  }
}

function extractError(err: unknown): string | null {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: { detail?: string } } }).response
    if (resp?.data?.detail) return resp.data.detail
  }
  return err instanceof Error ? err.message : null
}

function formatBytes(size: number | null): string {
  if (size === null || size <= 0) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`
  return `${(size / (1024 * 1024)).toFixed(1)} MB`
}

function formatDate(isoString: string): string {
  return formatDateTime(isoString, locale.value, timeZone.value)
}

function sourceLabel(source: string): string {
  switch (source) {
    case 'tool':
      return t('albumPanel.source.tool')
    case 'stage':
      return t('albumPanel.source.stage')
    case 'upload':
      return t('albumPanel.source.upload')
    default:
      return source
  }
}

// Reload on character change
watch(() => props.characterId, reload, { immediate: true })

defineExpose({ reload })
</script>

<template>
  <div class="album-panel">
    <div class="album-header">
      <h3 class="section-title">{{ t('albumPanel.title') }}</h3>
      <p class="album-hint">
        {{ t('albumPanel.hint') }}
      </p>
    </div>

    <div v-if="!characterId" class="album-empty">
      {{ t('albumPanel.noCharacter') }}
    </div>
    <div v-else-if="loading" class="album-empty">{{ t('common.state.loading') }}</div>
    <div v-else-if="items.length === 0" class="album-empty">
      {{ t('albumPanel.empty') }}
    </div>
    <div v-else class="album-grid">
      <div
        v-for="item in items"
        :key="item.id"
        class="album-tile"
      >
        <a
          :href="item.url"
          target="_blank"
          rel="noopener noreferrer"
          class="album-image-link"
          :title="item.caption ?? ''"
        >
          <img
            :src="item.url"
            :alt="item.caption ?? t('albumPanel.imageAlt')"
            loading="lazy"
          />
        </a>
        <div class="album-meta">
          <div class="album-caption" :title="item.caption ?? ''">
            {{ item.caption || t('albumPanel.noCaption') }}
          </div>
          <div class="album-sub">
            <span class="album-source">{{ sourceLabel(item.source) }}</span>
            <span class="album-sep">·</span>
            <span>{{ formatDate(item.created_at) }}</span>
            <span v-if="item.byte_size" class="album-sep">·</span>
            <span v-if="item.byte_size">{{ formatBytes(item.byte_size) }}</span>
          </div>
          <div class="album-actions">
            <UiButton
              size="sm"
              class="album-action-btn"
              :disabled="busyItemId === item.id"
              :title="t('albumPanel.actions.promoteTitle')"
              @click="handlePromote(item)"
            >{{ t('albumPanel.actions.promote') }}</UiButton>
            <UiButton
              variant="danger"
              size="sm"
              class="album-action-btn"
              :disabled="busyItemId === item.id"
              :title="t('albumPanel.actions.deleteTitle')"
              @click="handleDelete(item)"
            >{{ t('albumPanel.actions.delete') }}</UiButton>
          </div>
        </div>
      </div>
    </div>

    <div v-if="errorMsg" class="album-error">{{ errorMsg }}</div>
  </div>
</template>

<style scoped>
.album-panel {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.album-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
  letter-spacing: 0.5px;
  margin: 0;
}

.album-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  margin: 0;
}

.album-empty {
  padding: 16px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
}

.album-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 10px;
}

.album-tile {
  display: flex;
  flex-direction: column;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  overflow: hidden;
}

.album-image-link {
  display: block;
  aspect-ratio: 3 / 4;
  overflow: hidden;
  background: var(--color-surface);
}

.album-image-link img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.15s;
}

.album-image-link:hover img {
  transform: scale(1.03);
}

.album-meta {
  padding: 6px 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-height: 0;
}

.album-caption {
  font-size: 12px;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.album-sub {
  font-size: 10px;
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.album-source {
  color: var(--color-primary-light);
}

.album-sep {
  margin: 0 4px;
  opacity: 0.5;
}

.album-actions {
  display: flex;
  gap: 4px;
  margin-top: 2px;
}

.album-action-btn {
  flex: 1;
}

.album-error {
  padding: 8px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}
</style>
