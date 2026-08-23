<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiBadge, UiButton, UiImage, UiLightbox } from '@/components/ui'
import type { LightboxItem } from '@/components/ui'
import {
  canInstallCard,
  isCloudCard,
  shouldShowCloudOnlyNotice,
} from '@/utils/characterCardSource'
import type { CharacterCardPreview } from '@/utils/api/characters'

const props = withDefaults(
  defineProps<{
    card: CharacterCardPreview
    actionLabel?: string
    actionLoading?: boolean
    actionDisabled?: boolean
  }>(),
  {
    actionLabel: '',
    actionLoading: false,
    actionDisabled: false,
  },
)

const emit = defineEmits<{
  action: []
}>()

const { t } = useI18n()

const detailsOpen = ref(false)
const activeImageIndex = ref(0)
const failedImageIndexes = ref<Set<number>>(new Set())
const zoomOpen = ref(false)
const zoomIndex = ref(0)

const title = computed(() => props.card.name || props.card.title)
const intro = computed(() => props.card.summary || props.card.description)
const activeImage = computed(() => props.card.image_urls[activeImageIndex.value] ?? '')
const showImage = computed(() => activeImage.value && !failedImageIndexes.value.has(activeImageIndex.value))
const initial = computed(() => (props.card.name || title.value || '?').trim().charAt(0).toUpperCase())
/** 浮窗的集合＝這張卡全部的圖，跟卡面 dots 切的是同一份 `image_urls`。 */
const lightboxItems = computed<LightboxItem[]>(() =>
  props.card.image_urls.map((url) => ({ url })),
)

// A cloud-exclusive card on a deployment that cannot install it: the card
// stays on the shelf, the button stops pretending. Both come from the
// backend's own verdict — the browser cannot see the credential that decides
// it (EC4).
const installBlocked = computed(() => !canInstallCard(props.card))
const showCloudOnly = computed(() => shouldShowCloudOnlyNotice(props.card))

const detailRows = computed(() => {
  const rows: Array<{ label: string; value: string }> = []
  // A cloud card's response body carries no structural settings — the
  // catalog only publishes prose and images (OC6g). The fields below sit at
  // the shared DTO's non-empty defaults on a cloud card (a "medium" band on
  // every disposition axis, "modern" world, 3/day proactive cadence…), which
  // would read as facts about the character if shown. Skip them for a cloud
  // card; they become real once the card is installed and the `.lumecard`
  // itself is unpacked.
  const structural = !isCloudCard(props.card)
  addRow(rows, 'summary', props.card.summary)
  addListRow(rows, 'personality', props.card.personality)
  addListRow(rows, 'interests', props.card.interests)
  addRow(rows, 'speakingStyle', props.card.speaking_style)
  addListRow(rows, 'boundaries', props.card.boundaries)
  addListRow(rows, 'aspirations', props.card.aspirations)
  addRow(rows, 'appearance', props.card.appearance)
  addRow(rows, 'genderIdentity', props.card.gender_identity)
  addRow(rows, 'pronoun', props.card.third_person_pronoun)
  addRow(rows, 'visualPresentation', props.card.visual_gender_presentation)
  if (structural) {
    addRow(
      rows,
      'visualSubjectType',
      props.card.visual_subject_type
        ? t(`characterCreate.fields.visualSubjectType.options.${props.card.visual_subject_type}`)
        : '',
    )
    addRow(rows, 'birthday', props.card.date_of_birth ?? '')
    addRow(rows, 'worldFrame', props.card.world_frame)
  }
  addListRow(rows, 'worldTopics', props.card.world_topics)
  addListRow(rows, 'subscribedCategories', props.card.subscribed_categories)
  addListRow(rows, 'excludedTopics', props.card.excluded_topics)
  if (structural) {
    addRow(rows, 'disposition', dispositionLabel.value)
  }
  addRow(rows, 'personalityType', personalityTypeLabel.value)
  if (structural) {
    addRow(rows, 'cadence', cadenceLabel.value)
  }
  addListRow(rows, 'companions', props.card.companions.map((c) => c.role ? `${c.name} (${c.role})` : c.name))
  if (structural) {
    addListRow(rows, 'arcTitles', props.card.bundled_arc_titles)
    addListRow(rows, 'arcSeriesTitles', props.card.bundled_arc_series_titles)
    if (props.card.bundled_arc_series_member_count) {
      addRow(
        rows,
        'arcSeriesMembers',
        String(props.card.bundled_arc_series_member_count),
      )
    }
  }
  addRow(rows, 'note', props.card.note)
  return rows
})

const dispositionLabel = computed(() => {
  const entries = [
    ['self_centeredness', props.card.disposition.self_centeredness],
    ['candor', props.card.disposition.candor],
    ['sharing_drive', props.card.disposition.sharing_drive],
    ['associativeness', props.card.disposition.associativeness],
  ] as const
  return entries.map(([dimension, band]) => (
    `${t(`playerSidebar.characterCards.details.dispositionDimensions.${dimension}`)}: ${t(`playerSidebar.characterCards.details.dispositionBands.${band}`)}`
  )).join(' / ')
})

const personalityTypeLabel = computed(() => {
  const type = props.card.personality_type
  if (!type?.code) return ''
  return type.rationale ? `${type.code} - ${type.rationale}` : type.code
})

const cadenceLabel = computed(() => props.card.proactive_enabled
  ? t('playerSidebar.characterCards.details.cadenceEnabled', {
    daily: props.card.proactive_daily_limit,
    cooldown: props.card.proactive_cooldown_minutes,
  })
  : t('playerSidebar.characterCards.details.cadenceDisabled'))

// 判準用「這張卡目前顯示哪些圖」而不是物件識別（FIX-C）：`browseCards`
// computed（`PlayerCharacterCardPanel.vue`）在翻譯（`setBrowseTranslate`）或
// 背景細節請求（`ensureActiveBrowseCardDetailed`）回來時，會拿同一張卡的新
// 資料重建一個新的 `CharacterCardPreview` 物件——`pack_id`／`image_urls` 都
// 沒變，只是物件識別換了。原本 `watch(() => props.card, ...)` 抓的正是物件
// 識別，於是玩家在瀏覽 modal 開著翻譯、等待期間點卡面圖放大，翻譯一回來就
// 被這條 watch 誤判成「換卡片」，浮窗和翻到第幾張一起被無預警關掉。
//
// 真正決定這條 watch 該不該重置的，是 `activeImageIndex` 索引進去的是不是
// 同一份圖清單：只要 `image_urls` 內容沒變，索引指的仍是正確的圖，重置
// （尤其是關掉浮窗）就沒有必要、只會打斷玩家。這跟 `ChatBubble.vue` 附件
// watch（同一份理由：`imageAttachments.value.map(att => att.url).join('\n')`）
// 用的是同一招。
//
// FIX-E：判準換寬之後多出一個缺口——「換到另一張 `image_urls` 內容相同的卡」
// 不再重置。所以再併上 `pack_id`。**只能是 `pack_id`**：
//
// - `name` / `title` / `summary` 一律不行。`name` 正是翻譯會改寫的欄位之一
//   （後端 `PROFILE_SCALAR_FIELDS` 明列 name／summary／speaking_style…），
//   把它放進鍵就等於把 FIX-C 修掉的那個 bug 原樣裝回去。
// - `pack_id` 是翻譯與細節回填的**字典鍵本身**（`translatedBrowseCards[pack_id]`
//   / `enrichedBrowseCards[pack_id]`），同一張卡的新物件必然帶同一個值，所以
//   它不會誤判；不同 pack 則必然不同，所以它擋得住「兩張都沒有圖的卡原地互換」。
//
// 仍有一個 `pack_id` 蓋不到的角落：`StudioCardsPage` 的預覽卡 `pack_id` 恆為
// `null`（它是從 `Character` 現組的，那個 DTO 裡根本沒有身分欄位），兩個都沒
// 有圖的角色互換時這條 watch 仍不會醒。那一邊改由頁面自己出身分——它有
// `selectedCharacterId`，`<CharacterCardFace :key>` 直接重掛，比在這裡猜一個
// 湊合的身分欄位誠實。
watch(() => `${props.card.pack_id ?? ''}\n${props.card.image_urls.join('\n')}`, () => {
  detailsOpen.value = false
  activeImageIndex.value = 0
  failedImageIndexes.value = new Set()
  // 換卡片時浮窗必須關掉：索引指向的是舊卡片那份 image_urls，留著會在新卡片
  // 落地的瞬間變成「同一格位置的另一張卡的圖」。`CharacterCardGalleryModal`
  // 用 `:key="activeIndex"` 整段重掛這個元件，換卡時通常不會走到這裡；但
  // `StudioCardsPage` 是原地換 `props.card`（無 key 重掛），這條 watch 是那
  // 邊唯一會清掉浮窗狀態的地方。
  zoomOpen.value = false
})

function addRow(
  rows: Array<{ label: string; value: string }>,
  key: string,
  value: string,
) {
  const cleaned = value.trim()
  if (!cleaned) return
  rows.push({
    label: t(`playerSidebar.characterCards.details.fields.${key}`),
    value: cleaned,
  })
}

function addListRow(
  rows: Array<{ label: string; value: string }>,
  key: string,
  values: string[],
) {
  const joined = values.map((value) => value.trim()).filter(Boolean).join(t('common.listSeparator'))
  if (!joined) return
  rows.push({
    label: t(`playerSidebar.characterCards.details.fields.${key}`),
    value: joined,
  })
}

function selectImage(index: number) {
  activeImageIndex.value = index
}

function markImageFailed() {
  const next = new Set(failedImageIndexes.value)
  next.add(activeImageIndex.value)
  failedImageIndexes.value = next
}

/**
 * 從卡面目前顯示的那張圖開始放大。`showImage` 為假（載入失敗、走 initial
 * 字母 fallback）時觸發鍵根本不會渲染，這裡的判斷只是防禦。
 */
function openZoom() {
  if (!showImage.value) return
  zoomIndex.value = activeImageIndex.value
  zoomOpen.value = true
}

// 浮窗裡換圖（←→ 鍵或點導覽鍵）要回寫卡面的 dots，關窗後卡面停在玩家最後
// 看到的那張，而不是彈回原本點進浮窗前的那張。
watch(zoomIndex, (index) => {
  activeImageIndex.value = index
})
</script>

<template>
  <article class="character-card-face">
    <div class="character-card-face__inner">
      <header class="character-card-face__nameplate">
        <h3 class="character-card-face__title">{{ title }}</h3>
        <span v-if="card.author" class="character-card-face__author">
          {{ card.author }}
        </span>
      </header>

      <div class="character-card-face__art">
        <!-- The card is capped at 320px; the art window gives back 7px of
             foil padding and 12px of margin either side, so 282px is its
             ceiling. `@error` still reaches `markImageFailed` — UiImage
             re-emits it — and the `v-else` initial is the real fallback.
             LB7：只有真的畫得出圖時才給放大入口——initial fallback 沒有東西
             可以放大。button 只包住圖片本身，光澤層仍是圖片的手足，不受影響。 -->
        <button
          v-if="showImage"
          type="button"
          class="character-card-face__art-trigger"
          :title="t('common.actions.zoom')"
          :aria-label="t('common.actions.zoomAria', { name: title })"
          @click="openZoom"
        >
          <UiImage
            class="character-card-face__image"
            variant="content"
            :src="activeImage"
            :alt="title"
            sizes="282px"
            aspect-ratio="3 / 4"
            @error="markImageFailed"
          />
        </button>
        <div v-else class="character-card-face__fallback" aria-hidden="true">
          {{ initial }}
        </div>

        <!-- pointer-events: none（見下方樣式）——光澤層蓋在圖上純視覺，不得
             吃掉放大鍵的點擊。 -->
        <span class="character-card-face__sheen" aria-hidden="true" />

        <div v-if="card.image_urls.length > 1" class="character-card-face__dots">
          <button
            v-for="(_url, index) in card.image_urls"
            :key="`${card.name}-${index}`"
            type="button"
            class="character-card-face__dot"
            :class="{ 'is-active': index === activeImageIndex }"
            :aria-label="t('playerSidebar.characterCards.gallery.imagePage', {
              current: index + 1,
              total: card.image_urls.length,
            })"
            @click="selectImage(index)"
          />
        </div>
      </div>

      <div class="character-card-face__body">
        <div
          v-if="card.tags.length || showCloudOnly"
          class="character-card-face__tags"
        >
          <UiBadge v-if="showCloudOnly" variant="warning">
            {{ t('playerSidebar.characterCards.cloudOnly.chip') }}
          </UiBadge>
          <UiBadge v-for="tag in card.tags" :key="tag" variant="default">
            {{ tag }}
          </UiBadge>
        </div>

        <p v-if="intro" class="character-card-face__intro">{{ intro }}</p>

        <div
          v-if="card.has_main_arc || card.bundled_arc_template_count || card.has_arc_series || card.stage_image_count"
          class="character-card-face__badges"
        >
          <UiBadge v-if="card.has_main_arc || card.bundled_arc_template_count" variant="warning">
            {{ t('playerSidebar.characterCards.storySeedCount', {
              count: card.bundled_arc_template_count,
            }) }}
          </UiBadge>
          <UiBadge v-if="card.has_arc_series || card.bundled_arc_series_count" variant="success">
            {{ t('playerSidebar.characterCards.arcSeriesCount', {
              count: card.bundled_arc_series_count,
            }) }}
          </UiBadge>
          <UiBadge v-if="card.stage_image_count" variant="primary">
            {{ t('playerSidebar.characterCards.stageImageCount', {
              count: card.stage_image_count,
            }) }}
          </UiBadge>
        </div>

        <button
          v-if="detailRows.length"
          type="button"
          class="character-card-face__details-toggle"
          :aria-expanded="detailsOpen"
          @click="detailsOpen = !detailsOpen"
        >
          {{ detailsOpen
            ? t('playerSidebar.characterCards.details.hide')
            : t('playerSidebar.characterCards.details.show') }}
        </button>

        <dl v-if="detailsOpen" class="character-card-face__details">
          <div
            v-for="row in detailRows"
            :key="row.label"
            class="character-card-face__detail-row"
          >
            <dt>{{ row.label }}</dt>
            <dd>{{ row.value }}</dd>
          </div>
        </dl>

        <p v-if="showCloudOnly" class="character-card-face__cloud-only">
          {{ t('playerSidebar.characterCards.cloudOnly.hint') }}
        </p>

        <UiButton
          v-if="actionLabel"
          variant="primary"
          block
          :loading="actionLoading"
          :disabled="actionDisabled || installBlocked"
          @click="emit('action')"
        >
          {{ actionLabel }}
        </UiButton>
      </div>
    </div>

    <!-- LB7：浮窗自己 Teleport 到 body、z-index 1500，疊放位置與這張卡在
         DOM 裡的哪個位置無關；放在 article 裡只是讓它跟卡片同生命週期。
         鍵盤打架（Esc／←→ 同時被 CharacterCardGalleryModal 的 window 監聽
         攔截）由浮窗自己解：UiLightbox 以 capture 階段掛 window keydown，
         對它處理的鍵 stopPropagation()，事件到不了下面那層 bubble 監聽。
         這裡與上層都不需要（也不該有）手寫讓位。 -->
    <UiLightbox
      v-model:index="zoomIndex"
      :visible="zoomOpen"
      :items="lightboxItems"
      :label="title"
      @close="zoomOpen = false"
    />
  </article>
</template>

<style scoped>
/* 收藏卡質感：金屬 foil 描邊（padding 留出邊框）包住內層暗色卡身。 */
.character-card-face {
  position: relative;
  width: min(320px, 100%);
  padding: 7px;
  border-radius: 18px;
  background:
    linear-gradient(
      150deg,
      rgba(255, 209, 128, 0.85),
      rgba(201, 143, 219, 0.6) 30%,
      rgba(139, 109, 255, 0.5) 52%,
      rgba(255, 209, 128, 0.42) 100%
    );
  box-shadow:
    0 18px 54px rgba(0, 0, 0, 0.46),
    0 2px 0 rgba(255, 255, 255, 0.14) inset;
  transition: transform 0.28s ease, box-shadow 0.28s ease;
}

.character-card-face:hover {
  transform: translateY(-3px);
  box-shadow:
    0 26px 68px rgba(0, 0, 0, 0.54),
    0 0 0 1px rgba(255, 209, 128, 0.5),
    0 2px 0 rgba(255, 255, 255, 0.18) inset;
}

.character-card-face__inner {
  display: flex;
  flex-direction: column;
  border-radius: 12px;
  background:
    radial-gradient(120% 60% at 50% -8%, rgba(255, 209, 128, 0.16), transparent 60%),
    linear-gradient(180deg, rgba(33, 24, 64, 0.98), rgba(17, 13, 36, 0.99));
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.4) inset;
  overflow: hidden;
}

.character-card-face__nameplate {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
  padding: 10px 14px 8px;
  border-bottom: 1px solid rgba(255, 209, 128, 0.22);
  background: linear-gradient(180deg, rgba(255, 209, 128, 0.1), transparent);
}

.character-card-face__title {
  min-width: 0;
  margin: 0;
  color: var(--color-text);
  font-family: var(--font-display);
  font-size: 23px;
  letter-spacing: 0.4px;
  line-height: 1.2;
  font-weight: 600;
  overflow-wrap: anywhere;
  text-shadow: 0 1px 8px rgba(255, 209, 128, 0.18);
}

.character-card-face__author {
  flex-shrink: 0;
  color: var(--color-spark);
  font-size: var(--font-xs);
  letter-spacing: 0.3px;
  opacity: 0.85;
}

/* 內嵌藝術窗：內框 + 內陰影，框出主圖。 */
.character-card-face__art {
  position: relative;
  margin: 10px 12px 0;
  aspect-ratio: 3 / 4;
  border-radius: 9px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.04);
  box-shadow:
    0 0 0 1px rgba(255, 255, 255, 0.16) inset,
    0 8px 22px rgba(0, 0, 0, 0.42);
}

/* 放大入口：只包住圖片本身，把原生 button 的邊框/底色/內距清乾淨，讓它看起來
   還是那張圖，不是一顆按鈕。 */
.character-card-face__art-trigger {
  display: block;
  width: 100%;
  height: 100%;
  padding: 0;
  margin: 0;
  border: 0;
  background: transparent;
  cursor: zoom-in;
  appearance: none;
}

.character-card-face__art-trigger:focus-visible {
  outline: 2px solid var(--color-primary);
  outline-offset: -2px;
}

.character-card-face__image {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.character-card-face__fallback {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  background:
    radial-gradient(circle at 36% 24%, rgba(255, 209, 128, 0.4), transparent 36%),
    linear-gradient(135deg, rgba(95, 213, 164, 0.22), rgba(139, 109, 255, 0.28)),
    rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-family: var(--font-display);
  font-size: 74px;
  line-height: 1;
  text-shadow: 0 2px 14px rgba(0, 0, 0, 0.4);
}

/* 全像光澤：斜向高光帶，懸停時掃過卡面。 */
.character-card-face__sheen {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: linear-gradient(
    122deg,
    transparent 32%,
    rgba(255, 255, 255, 0.22) 46%,
    rgba(201, 143, 219, 0.18) 52%,
    transparent 66%
  );
  background-size: 250% 250%;
  background-position: 0% 0%;
  mix-blend-mode: screen;
  opacity: 0.65;
  transition: background-position 0.9s ease;
}

.character-card-face:hover .character-card-face__sheen {
  background-position: 100% 100%;
}

.character-card-face__dots {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 10px;
  display: flex;
  justify-content: center;
  gap: 6px;
}

.character-card-face__dot {
  width: 8px;
  height: 8px;
  border: 1px solid rgba(255, 255, 255, 0.8);
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.42);
  padding: 0;
  cursor: pointer;
  transition: transform 0.15s ease;
}

.character-card-face__dot:hover {
  transform: scale(1.2);
}

.character-card-face__dot.is-active {
  background: var(--color-spark);
  border-color: var(--color-spark);
  box-shadow: 0 0 8px rgba(255, 209, 128, 0.7);
}

.character-card-face__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
}

.character-card-face__tags,
.character-card-face__badges {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

/* 描述框：內嵌面板，仿卡牌底部說明欄。完整顯示，過長交給 modal 捲動。 */
.character-card-face__intro {
  margin: 0;
  padding: 8px 10px;
  border-radius: 7px;
  border: 1px solid rgba(255, 255, 255, 0.07);
  background: rgba(255, 255, 255, 0.035);
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  line-height: 1.6;
  white-space: pre-line;
  overflow-wrap: anywhere;
}

/* 「僅雲端版」說明：不是錯誤，是這張卡的事實，所以走提示色而非錯誤色。 */
.character-card-face__cloud-only {
  margin: 0;
  padding: 8px 10px;
  border-radius: 7px;
  border: 1px solid rgba(255, 209, 128, 0.28);
  background: rgba(255, 209, 128, 0.08);
  color: #ffd180;
  font-size: var(--font-xs);
  line-height: 1.6;
}

.character-card-face__details-toggle {
  align-self: flex-start;
  border: 0;
  background: transparent;
  color: var(--color-primary-light);
  cursor: pointer;
  font: inherit;
  font-size: var(--font-xs);
  letter-spacing: 0.2px;
  padding: 2px 0;
}

.character-card-face__details-toggle:hover {
  color: var(--color-spark);
  text-decoration: underline;
}

.character-card-face__details {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 220px;
  overflow-y: auto;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.035);
}

.character-card-face__detail-row {
  display: grid;
  gap: 2px;
}

.character-card-face__detail-row dt {
  color: var(--color-spark);
  font-size: var(--font-xs);
  font-weight: 650;
  letter-spacing: 0.2px;
}

.character-card-face__detail-row dd {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: reduce) {
  .character-card-face,
  .character-card-face__sheen,
  .character-card-face__dot {
    transition: none;
  }

  .character-card-face:hover {
    transform: none;
  }
}
</style>
