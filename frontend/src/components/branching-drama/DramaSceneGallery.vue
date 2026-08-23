<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton, UiImage, UiLightbox } from '@/components/ui'
import type { DramaSceneGallery } from '@/types/branchingDrama'
import {
  buildGalleryTiles,
  DRAMA_SCENE_ASPECT_RATIO,
  galleryLightboxIndex,
  galleryLightboxItems,
  type DramaGalleryTile,
} from '@/utils/dramaGallery'
import { formatDramaCompletionPercent, type DramaProgress } from '@/utils/dramaProgress'
import DramaProgressTree from '@/components/branching-drama/DramaProgressTree.vue'

/**
 * 劇場圖集 (BD9) — the drama's pictures as a collection.
 *
 * A branching tree paints far more than one playthrough ever shows: the
 * prefetch draws branches nobody entered, and every replay fills in more.
 * This view turns that into something worth going back for — the pictures
 * you walked past in full, everything else as a locked silhouette that says
 * only "there is more here".
 *
 * Presentational: the fetch belongs to the page above. The silhouette holds
 * no data to hide because the server never sent any (see
 * `types/branchingDrama.DramaSceneGallery`) — this component could not leak
 * a locked title if it tried, which is the point.
 */
const props = defineProps<{
  gallery: DramaSceneGallery | null
  /**
   * How much of the *tree* this player has walked (D8.1), computed by the
   * page from the sessions it already holds — this view has no access to
   * them, and the gallery payload deliberately cannot answer the question
   * (its numbers only ever describe pictures that exist).
   *
   * The whole `DramaProgress` rather than just the percentage, so the
   * branching graph (BD14) can hang in this panel off the same object and
   * cannot end up describing a different drama than the header line does.
   * `null` while nothing is selected.
   */
  progress?: DramaProgress | null
  /**
   * The playthrough the branching graph burns gold — on this page, the most
   * recent one, decided by the page rather than here (BD14). `null` when
   * there is none to point at.
   */
  currentSessionId?: string | null
  loading?: boolean
  errorMessage?: string | null
}>()

defineEmits<{ (e: 'close'): void }>()

const { t } = useI18n()

const tiles = computed<DramaGalleryTile[]>(() =>
  props.gallery ? buildGalleryTiles(props.gallery) : [],
)

/**
 * The completion line, or `null` when there is no honest one to print — no
 * drama selected, or a segment count this client could not read into a tree.
 * A denominator of zero is never rendered as 「0 / 0」.
 */
const completion = computed(() => {
  const value = props.progress?.completion
  return value && value.total > 0 ? value : null
})

/**
 * Nothing to lay out *and* nothing to explain. A failed load with no data
 * is not "this drama has no pictures yet" — the error line says what
 * happened, and printing the empty copy underneath it would contradict it.
 */
const isEmpty = computed(
  () => !props.errorMessage && tiles.value.length === 0,
)

// -------------------------------------------------------------- 放大檢視 (LB6)

/**
 * 放大後可以左右翻的那一疊。**只有已收集的格子**——鎖定格連 `imageUrl` 都
 * 沒有（後端不下發），把它排進集合等於讓左右鍵走進一頁空白，也等於把未走過的
 * 節點排成一條可枚舉的序列。集合裡沒有它們，導覽就自然跳過。
 * 篩選與座標映射都在 `utils/dramaGallery`，因為 SSR harness 測得到純函式、
 * 測不到點擊（見該檔註解）。
 */
const zoomItems = computed(() => galleryLightboxItems(tiles.value))
const zoomOpen = ref(false)
const zoomIndex = ref(0)

function openTile(gridIndex: number) {
  const at = galleryLightboxIndex(tiles.value, gridIndex)
  if (at < 0) return
  zoomIndex.value = at
  zoomOpen.value = true
}

/**
 * 換一齣戲（或整份重抓）就關窗。索引指的是上一份集合，留著就會在新集合落地的
 * 瞬間變成「同一格位置的另一張圖」——沿 `AlbumPanel` 換角色時的既有處置。
 */
watch(
  () => props.gallery,
  () => {
    zoomOpen.value = false
    zoomIndex.value = 0
  },
)
</script>

<template>
  <section class="drama-gallery">
    <header class="drama-gallery__head">
      <div class="drama-gallery__heading">
        <h3>{{ t('branchingDrama.gallery.title') }}</h3>
        <p v-if="completion" class="drama-gallery__count">
          {{ t('branchingDrama.gallery.completion', {
            percent: formatDramaCompletionPercent(completion),
            walked: completion.walked,
            total: completion.total,
          }) }}
        </p>
      </div>
      <UiButton size="sm" variant="ghost" @click="$emit('close')">
        {{ t('branchingDrama.gallery.close') }}
      </UiButton>
    </header>

    <p class="drama-gallery__hint">{{ t('branchingDrama.gallery.hint') }}</p>

    <!-- 分歧圖 (D8.3)：跟完成度數字同源（同一個 progress），所以亮起來
         的面積跟旁邊那個百分比永遠在講同一件事。只有點與邊，沒有標題、縮
         圖或可點擊目標——防劇透在這張圖上是「根本沒那些資料」而不是「藏起來」。 -->
    <DramaProgressTree
      v-if="progress"
      class="drama-gallery__tree"
      :progress="progress"
      :current-session-id="currentSessionId ?? null"
    />

    <!-- A refresh that failed is a banner, not a blank page: whatever was
         already collected stays on screen underneath it. -->
    <p v-if="errorMessage" class="drama-gallery__state is-error" role="alert">
      {{ errorMessage }}
    </p>

    <p v-if="loading && !tiles.length" class="drama-gallery__state">
      {{ t('branchingDrama.gallery.loading') }}
    </p>
    <p v-else-if="isEmpty" class="drama-gallery__state">
      {{ t('branchingDrama.gallery.empty') }}
    </p>

    <ul v-else-if="tiles.length" class="drama-gallery__grid">
      <li
        v-for="(tile, gridIndex) in tiles"
        :key="tile.key"
        class="drama-gallery__cell"
      >
        <button
          v-if="tile.kind === 'collected'"
          type="button"
          class="drama-gallery__tile"
          :title="tile.title"
          :data-tone="tile.tone ?? 'root'"
          @click="openTile(gridIndex)"
        >
          <!-- `thumb` plans a 2/3 portrait box (character立繪); a scene is
               landscape, so the ratio is stated rather than inherited. -->
          <UiImage
            variant="thumb"
            :src="tile.imageUrl"
            :alt="tile.title"
            :aspect-ratio="DRAMA_SCENE_ASPECT_RATIO"
            sizes="(max-width: 640px) 45vw, 160px"
          />
          <span class="drama-gallery__caption">{{ tile.title }}</span>
        </button>
        <!-- 鎖定格：只說「這裡還有一張」。標題、摘要、圖都不在手上，
             因為後端根本沒送——防劇透是 payload 的事，不是這裡的事。 -->
        <div
          v-else
          class="drama-gallery__tile is-locked"
          :aria-label="t('branchingDrama.gallery.lockedAria')"
        >
          <span class="drama-gallery__lock" aria-hidden="true">?</span>
          <span class="drama-gallery__caption">
            {{ t('branchingDrama.gallery.locked') }}
          </span>
        </div>
      </li>
    </ul>

    <!-- 放大看原圖。原本是這裡手刻的一層 overlay：只有一張圖、只認 Escape，
         看完一張要關掉再點下一張。改成共用浮窗 (LB6) 之後，玩家拿到左右鍵、
         橫滑、焦點管理、背景捲動鎖、手機返回鍵與「開原圖」。
         集合刻意只有已收集的格子——鎖定格不進導覽（見 `zoomItems`）。 -->
    <UiLightbox
      v-model:index="zoomIndex"
      :visible="zoomOpen"
      :items="zoomItems"
      :label="t('branchingDrama.gallery.title')"
      @close="zoomOpen = false"
    />
  </section>
</template>

<style scoped>
.drama-gallery {
  display: flex;
  flex-direction: column;
  gap: 10px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 14px;
  background: rgba(255, 255, 255, 0.03);
}

.drama-gallery__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.drama-gallery__heading h3 {
  margin: 0;
  font-size: 15px;
}
.drama-gallery__count {
  margin: 2px 0 0;
  font-size: 13px;
  color: var(--color-primary-light);
  font-variant-numeric: tabular-nums;
}
.drama-gallery__hint {
  margin: 0;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
  line-height: 1.5;
}
.drama-gallery__tree {
  margin-top: 2px;
}
.drama-gallery__state {
  margin: 0;
  padding: 12px 0;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.6);
}
.drama-gallery__state.is-error {
  color: var(--color-danger, #ff7875);
}

.drama-gallery__grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 10px;
}
.drama-gallery__cell {
  min-width: 0;
}

.drama-gallery__tile {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 0;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  overflow: hidden;
  background: rgba(0, 0, 0, 0.25);
  color: inherit;
  text-align: left;
  cursor: pointer;
}
.drama-gallery__tile:hover {
  border-color: rgba(var(--color-primary-rgb), 0.6);
}

/* Which branch this picture came off, as a hairline down the side. The tree
   only ever produces these three tones plus a toneless root, so the closed
   set is safe to enumerate in CSS — an unknown value simply gets no accent
   rather than a wrong one. */
.drama-gallery__tile[data-tone='dark'] {
  border-left: 3px solid rgba(140, 120, 200, 0.7);
}
.drama-gallery__tile[data-tone='sunny'] {
  border-left: 3px solid rgba(230, 190, 110, 0.7);
}
.drama-gallery__tile[data-tone='neutral'] {
  border-left: 3px solid rgba(140, 175, 190, 0.7);
}
.drama-gallery__tile[data-tone='root'] {
  border-left: 3px solid rgba(255, 255, 255, 0.35);
}
.drama-gallery__tile img {
  display: block;
  width: 100%;
  height: auto;
  object-fit: cover;
}

/* 剪影：同樣的格子大小、同樣的橫式比例，但畫面上什麼都沒有。
   比例跟著場景圖走（見 utils/dramaGallery 的 DRAMA_SCENE_ASPECT_RATIO）——
   一格直式剪影混在一排橫式場景裡，看起來會像壞掉而不是「還沒解鎖」。 */
.drama-gallery__tile.is-locked {
  cursor: default;
  align-items: center;
  justify-content: center;
  aspect-ratio: 3 / 2;
  background:
    repeating-linear-gradient(
      135deg,
      rgba(255, 255, 255, 0.05) 0 6px,
      rgba(255, 255, 255, 0.02) 6px 12px
    );
  border-style: dashed;
}
.drama-gallery__tile.is-locked:hover {
  border-color: rgba(255, 255, 255, 0.12);
}
.drama-gallery__lock {
  font-size: 26px;
  line-height: 1;
  color: rgba(255, 255, 255, 0.28);
}

.drama-gallery__caption {
  display: block;
  padding: 0 8px 8px;
  font-size: 12px;
  line-height: 1.4;
  color: rgba(255, 255, 255, 0.75);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.drama-gallery__tile.is-locked .drama-gallery__caption {
  padding: 6px 8px 8px;
  color: rgba(255, 255, 255, 0.4);
  text-align: center;
}

/* 放大檢視的樣式（含「一個比例兩個來源，所以要 contain 不能 fill」那條）
   已隨手刻 overlay 一起移交 `UiLightbox`——它的 `.ui-lightbox__image` 就是
   `object-fit: contain`，理由與這裡原本寫的同一條。 */

@media (max-width: 640px) {
  .drama-gallery__grid {
    grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
  }
}
</style>
