<script setup lang="ts">
/**
 * 角色相簿面板。
 *
 * 顯示該角色相簿圖片（工具生成 + 從舞台轉來），支援：
 * - 點圖預覽（新分頁開啟原圖）
 * - 刪除（檔案一起刪）
 * - 晉升為舞台圖（加回 image_urls）
 * - 往下捲載入更多（keyset 分頁，）
 *
 * 跟 CharacterImagesPanel 分開：此面板處理的是「長期收藏」，
 * 舞台面板處理的是「目前輪播中」。兩邊互相移動資料。
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlayerCopy } from '@/composables/usePlayerCopy'

import type { AlbumItem } from '@/types/album'
import { UiBadge, UiButton, UiImage } from '@/components/ui'
import {
  deleteAlbumItem,
  listAlbum,
  promoteAlbumToStage,
} from '@/utils/api/album'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { formatDateTime } from '@/i18n/formatters'
import { safeMediaHref } from '@/utils/safeMediaUrl'

const props = defineProps<{
  characterId: string | null
  /**
   * EC2-C: `image_urls` is licensor-owned on a managed (IP-partner)
   * character — the server refuses this panel's own "promote to stage"
   * write path (`AlbumManagedError`, same family as
   * `CharacterCardManagedError`) exactly like it already refuses PATCH.
   * The button is hidden rather than left to round-trip a 403, matching
   * every other EC2-B/EC2-C managed-character gate. Deletion is
   * untouched — the album is the player's own collection either way.
   */
  managed?: boolean
}>()

const emit = defineEmits<{
  /** 晉升 / 舞台→相簿轉移後，通知上層刷新 character */
  characterUpdated: [characterId: string]
}>()

const { t, locale } = useI18n()
const { pt } = usePlayerCopy()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()

const items = ref<AlbumItem[]>([])
const total = ref(0)
const hasMore = ref(false)
const nextBefore = ref<string | null>(null)
const loading = ref(false)
const loadingMore = ref(false)
const busyItemId = ref<string | null>(null)
const errorMsg = ref<string | null>(null)
const sentinel = ref<HTMLElement | null>(null)

/** 換角色即重抓第一頁，捨棄先前的分頁狀態。 */
async function reload() {
  hasMore.value = false
  nextBefore.value = null
  if (!props.characterId) {
    items.value = []
    total.value = 0
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    const res = await listAlbum(props.characterId)
    items.value = res.items
    total.value = res.total
    hasMore.value = res.has_more
    nextBefore.value = res.next_before
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('albumPanel.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

/** 捲到底時載入下一頁（keyset：帶上前一頁最舊一張的 created_at）。 */
async function loadMore() {
  if (!props.characterId || !hasMore.value) return
  if (loading.value || loadingMore.value) return
  loadingMore.value = true
  errorMsg.value = null
  try {
    const res = await listAlbum(props.characterId, { before: nextBefore.value })
    items.value = [...items.value, ...res.items]
    total.value = res.total
    hasMore.value = res.has_more
    nextBefore.value = res.next_before
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('albumPanel.errors.loadFailed')
  } finally {
    loadingMore.value = false
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
    total.value = Math.max(0, total.value - 1)
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
    total.value = Math.max(0, total.value - 1)
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

// 往下捲載入更多：sentinel 進入視窗（含 200px 預載邊界）就取下一頁。
// sentinel 只在 v-if="items.length > 0" 時掛載（換角色清空列表時會被
// v-unmount 又重新掛回），故用 watch(sentinel) 動態重新 observe，而不
// 只在 onMounted 觀察一次。loadMore() 內部自己判斷 hasMore，所以就算
// 已無下一頁、sentinel 仍在畫面上也不會多打 API。
let observer: IntersectionObserver | null = null

function observeSentinel(el: Element | null) {
  if (!observer) return
  observer.disconnect()
  if (el) observer.observe(el)
}

onMounted(() => {
  if (typeof IntersectionObserver === 'undefined') return
  observer = new IntersectionObserver((entries) => {
    if (entries.some(entry => entry.isIntersecting)) {
      void loadMore()
    }
  }, { rootMargin: '200px' })
  observeSentinel(sentinel.value)
})

watch(sentinel, (el) => observeSentinel(el))

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
})

defineExpose({ reload })
</script>

<template>
  <div class="album-panel">
    <div class="album-header">
      <h3 class="section-title">{{ t('albumPanel.title') }}</h3>
      <p class="album-hint">
        {{ pt('albumPanel.hint') }}
      </p>
    </div>

    <!-- EC2-C：託管角色的舞台圖是授權方資產，「晉升為舞台圖」會把相簿圖片
         寫回 image_urls——伺服端已拒絕，這裡先一步不給誤按的機會。刪除不
         受影響，相簿本身仍是玩家自己的收藏。 -->
    <div v-if="managed" class="managed-album-notice">
      <UiBadge variant="primary">{{ t('characterEdit.managed.albumBadge') }}</UiBadge>
      <p class="managed-album-notice__text">{{ t('characterEdit.managed.albumNotice') }}</p>
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
          :href="safeMediaHref(item.url)"
          target="_blank"
          rel="noopener noreferrer"
          class="album-image-link"
          :title="item.caption ?? ''"
        >
          <UiImage
            :src="item.url"
            :alt="item.caption ?? t('albumPanel.imageAlt')"
            variant="thumb"
            sizes="140px"
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
              v-if="!managed"
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

    <div v-if="items.length > 0" ref="sentinel" class="album-sentinel" aria-hidden="true" />
    <div v-if="loadingMore" class="album-status">
      {{ t('albumPanel.pagination.loadingMore') }}
    </div>
    <div v-else-if="items.length > 0" class="album-status">
      {{ hasMore
        ? t('albumPanel.pagination.countSummary', { loaded: items.length, total })
        : t('albumPanel.pagination.allLoaded', { total }) }}
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

.managed-album-notice {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  background: rgba(126, 182, 255, 0.08);
  border: 1px solid rgba(126, 182, 255, 0.3);
  border-radius: 8px;
  padding: 12px;
}

.managed-album-notice__text {
  margin: 0;
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.6;
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

.album-sentinel {
  height: 1px;
}

.album-status {
  text-align: center;
  font-size: 11px;
  color: var(--color-text-secondary);
  padding: 4px 0;
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
