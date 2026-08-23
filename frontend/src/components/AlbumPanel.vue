<script setup lang="ts">
/**
 * 角色相簿面板。
 *
 * 顯示該角色相簿圖片（工具生成 + 從舞台轉來），支援：
 * - 點圖在浮窗中放大（`UiLightbox`，左右鍵在已載入的相簿項目之間連續切換）
 * - 刪除（檔案一起刪）
 * - 晉升為舞台圖（加回 image_urls）
 * - 往下捲載入更多（keyset 分頁，）
 *
 * 跟 CharacterImagesPanel 分開：此面板處理的是「長期收藏」，
 * 舞台面板處理的是「目前輪播中」。兩邊互相移動資料。
 *
 * LB2（計畫 `IMAGE_LIGHTBOX_PLAN.md`）：相簿是浮窗的第一個真實消費者，也是
 * 唯一集合會**非同步成長**的一個——走到已載入的最後一張還要往前時，浮窗會
 * 發 `load-more`，這裡把它接到既有的 `loadMore()`（跟往下捲用的是同一條路徑
 * 與同一組 keyset 游標，所以兩個入口不會各自打亂分頁狀態）。
 *
 * 集合會非同步成長，就代表「玩家按下下一張」與「那一張真的存在」中間隔著一次
 * HTTP 往返，而那段空窗裡玩家仍然可以操作。**那段空窗的規矩全部在浮窗自己
 * 身上**（等待記號、以及「玩家自己走了一步就不欠他了」的作廢），面板這裡只剩
 * 一件它獨有的事：
 *
 * - **續載失敗訊息綁「這次開窗」而不是綁面板**（`openZoom` / `closeZoom`）。
 *   綁面板會讓上一次開窗的紅字跟著下一次開窗一起出現。
 *
 * 這裡曾經多一套「只把補走那一步給還站在原地的人」的准駁（單向 `:index` ＋
 * 一個 flush 窗）。那是在浮窗被別的票佔用時的邊界補償：產生點仍在浮窗，面板
 * 只是在收下之前擋掉，而且靠 Vue 的 flush 順序才成立、下一個會非同步成長的
 * 消費者得整套重抄。根因修在浮窗之後（FIX-E），這裡回到單純的 `v-model:index`。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { usePlayerCopy } from '@/composables/usePlayerCopy'

import type { AlbumItem } from '@/types/album'
import { UiBadge, UiButton, UiImage, UiLightbox } from '@/components/ui'
import type { LightboxItem } from '@/components/ui'
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

// ---------------------------------------------------------------- 放大檢視

const zoomOpen = ref(false)
const zoomIndex = ref(0)
/**
 * 續載失敗要在浮窗裡自己說一次。
 *
 * 不重用下面那個面板層的 `errorMsg`：浮窗開著時整個面板都在 overlay 底下，
 * 玩家看不到它；而 `errorMsg` 同時也承載刪除／晉升的失敗，把它整條餵給浮窗
 * 等於讓浮窗顯示與續載無關的訊息，還會連帶讓浮窗的重試鍵重試錯的東西。
 */
const loadMoreError = ref<string | null>(null)

/**
 * 浮窗的集合＝**目前已載入的那些**。
 *
 * 刻意不預先展開到 `total`：沒載到的項目沒有 URL，放進來只會變成一格永遠
 * 空白的圖。走到末端時改用 `hasMore` 讓浮窗去要下一頁（見上方檔頭）。
 */
const lightboxItems = computed<LightboxItem[]>(() =>
  items.value.map(item => ({ url: item.url, caption: item.caption })),
)

function openZoom(at: number) {
  // 續載失敗訊息的生命週期綁「這次開窗」，不是綁面板。上一次開窗在末端續載失敗
  // 留下的紅字，會原封不動地出現在玩家接著點開的另一張圖底下——而當下根本沒有
  // 任何請求發生過；更糟的是那顆重試鍵還是活的，按下去續載成功就把索引往前推，
  // 玩家看的圖從他點的那張變成下一張。
  loadMoreError.value = null
  zoomIndex.value = at
  zoomOpen.value = true
}

/** 關窗與開窗對稱地收掉續載狀態，理由同 `openZoom`。 */
function closeZoom() {
  zoomOpen.value = false
  loadMoreError.value = null
}

/**
 * 浮窗走到已載入的末端了——去要下一頁，就這樣。
 *
 * `loadMore()` 本身不知道也不該知道浮窗的存在（往下捲的 sentinel 走的是同一
 * 支）；「這一頁回來之後要不要把玩家往前推一格」也不是這裡的事，浮窗自己記著
 * 那次續載是誰、從哪一張按出來的。
 */
function onLightboxLoadMore() {
  void loadMore()
}

/** 換角色即重抓第一頁，捨棄先前的分頁狀態。 */
async function reload() {
  // 換角色時浮窗必須關掉：它的索引指向的是舊角色那份清單，留著就會在新清單
  // 落地的瞬間變成「同一格位置的另一個人的圖」。
  zoomOpen.value = false
  zoomIndex.value = 0
  loadMoreError.value = null
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

/**
 * 載入下一頁（keyset：帶上前一頁最舊一張的 created_at）。
 *
 * 兩個入口共用這一支：往下捲的 sentinel，以及浮窗走到已載入末端時發的
 * `load-more`。共用是刻意的——兩邊各自維護游標會讓同一頁被要兩次。前面的
 * `loadingMore` 去重同時也是「玩家在載入中連按下一張」的擋板。
 */
async function loadMore() {
  if (!props.characterId || !hasMore.value) return
  if (loading.value || loadingMore.value) return
  loadingMore.value = true
  errorMsg.value = null
  loadMoreError.value = null
  try {
    const res = await listAlbum(props.characterId, { before: nextBefore.value })
    items.value = [...items.value, ...res.items]
    total.value = res.total
    hasMore.value = res.has_more
    nextBefore.value = res.next_before
  } catch (err) {
    const message = extractError(err) ?? t('albumPanel.errors.loadFailed')
    errorMsg.value = message
    // 浮窗開著時面板的錯誤列被 overlay 蓋住，所以同一則訊息也交給浮窗自己畫。
    loadMoreError.value = message
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
        v-for="(item, index) in items"
        :key="item.id"
        class="album-tile"
      >
        <!-- 原本是開新分頁的 anchor：連續看相簿要一張一張開再一張一張關，
             手機上每開一張多一個分頁。改成就地放大的浮窗（LB2）。仍是按鈕不是
             連結——它不導向任何地方；「看原檔／另存」那條路由浮窗自己保留。 -->
        <button
          type="button"
          class="album-image-button"
          :title="item.caption || t('common.actions.zoom')"
          :aria-label="t('common.actions.zoomAria', {
            name: item.caption || t('albumPanel.imageAlt'),
          })"
          @click="openZoom(index)"
        >
          <UiImage
            :src="item.url"
            :alt="item.caption ?? t('albumPanel.imageAlt')"
            variant="thumb"
            sizes="140px"
          />
        </button>
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

    <!-- 刪除／晉升刻意留在縮圖格子上（計畫 §3.3，primitive 不開 actions slot）。
         浮窗只有固定的「開原圖」與導覽。 -->
    <UiLightbox
      v-model:index="zoomIndex"
      :visible="zoomOpen"
      :items="lightboxItems"
      :has-more="hasMore"
      :loading-more="loadingMore"
      :load-more-error="loadMoreError"
      :label="t('albumPanel.title')"
      @close="closeZoom"
      @load-more="onLightboxLoadMore"
    />
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

/* 從 <a> 換成 <button>（LB2）。按鈕自帶的 padding / border / 系統底色要清掉，
   否則格子會多出一圈邊；鍵盤可達與 focus 環則是換掉 anchor 之後必須自己補回
   來的那一半——:focus-visible 內縮，才不會被 .album-tile 的 overflow 裁掉。 */
.album-image-button {
  display: block;
  width: 100%;
  padding: 0;
  border: none;
  aspect-ratio: 3 / 4;
  overflow: hidden;
  background: var(--color-surface);
  cursor: zoom-in;
}

.album-image-button img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.15s;
}

.album-image-button:hover img {
  transform: scale(1.03);
}

.album-image-button:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: -2px;
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
