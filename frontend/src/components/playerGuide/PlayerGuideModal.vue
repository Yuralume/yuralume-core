<script setup lang="ts">
/**
 * 玩家主介面「怎麼玩」總覽導覽（PG1）。
 *
 * 純靜態內容、零 API、零狀態：把散在各處的一次性提示（首輪引導、發話輔助
 * 發現提示、各種 coachmark）補上「隨時可以回來查」這一層。既有的就地提
 * 示全部保留——就地提示是當下推一把，這裡是回來查。
 *
 * 形式照 `studio/StudioGuideModal.vue`（Teleport + `canTeleport` SSR
 * fallback + backdrop + `role="dialog"` + Esc 關閉），差別只在十章太長，
 * 所以左側（行動版頂部）多一份章節目錄。
 *
 * 紅線：每一句都對應真的存在的按鈕或行為，逐章的來源如下——
 *
 *  - 第 1 章 聊天基本功：星號動作＝`ChatBubble` 的 `splitActionSegments`
 *    與 `infrastructure/prompt/default.py` 教角色的 `*...*` 慣例（**單**
 *    星號）；`/pic` ＝ `application/services/chat_service.py` 的
 *    `_FORCED_IMAGE_TRIGGER_RE`（獨立 token、不分大小寫；**只有混在其他
 *    文字裡時**指令字樣才不留在脈絡裡——`_resolve_image_trigger` 的
 *    fallback 在剝掉後為空時保留原訊息，所以單獨打 `/pic` 會原樣留著，
 *    文案不對 bare 用法做承諾）；同場／手機＝`ChatPanel` 的
 *    `chat-mode-bar`；傳圖＝`chat.input.attachImage` 那顆選單項與
 *    `ChatPanel.onPaste`（**沒有任何 drag-and-drop 監聽，別教拖放**）；
 *    語音＝`ChatBubble` 的播放鍵（`chat.bubble.ttsPlay` / `ttsReplay`，
 *    合成計費、重播吃 `cachedAudioUrl` 與伺服器端 `billable_quantity=0`
 *    所以不再扣）；發話輔助＝`chat.assist.*`；撤回＝
 *    `chat.actions.undo`；更早的訊息＝`chat.history.loadOlder`；NSFW mode
 *    ＝`NsfwModeSetting`（**自架限定**，`PersonalSettingsSection` 那一段
 *    整個 `v-if="!cloudMode"`；站台未設定時開關是「尚未開放」、閒置由
 *    `useNsfwMode` 的倒數自動熄燈、開著時 `NsfwModeAtmosphere` 蓋整頁）。
 *  - 第 2 章 讓故事動起來：起幕＝`StorySceneControl` / `useStoryScene`，
 *    價格語意對齊 `credits.price.storySceneTooltip`；建議行動＝
 *    `StorySceneChips`；示意＝`StageNudgeControl` 與
 *    `application/services/stage_nudge.py`。
 *  - 第 3 章 角色自己的生活：行程＝`PlayerScheduleCard`；補回與約定＝
 *    proactive-scheduling 的 busy-defer / promise；主動訊息節奏＝
 *    `ProactiveMessageSetting` 的安靜／適中／熱絡三檔。
 *  - 第 4 章 動態牆：`FeedPanel` / `FeedCard` / `KokoroGramOverlay`，紅點
 *    與推播＝`stage.launchers.kokoroGramTitleUnread` 與
 *    `playerSettings.webNotifications.*`。
 *  - 第 5 章 看圖與放大：整章對應 `ui/UiLightbox.vue` 與它的六個消費者
 *    （`AlbumPanel`、`ChatBubble`、`FeedCard`、`CharacterImagesPanel` 的舞台圖
 *    與生成候選、`CharacterCardFace`），計畫 `IMAGE_LIGHTBOX_PLAN.md`。這一章
 *    寫成獨立一章而不是散進四章，理由與骨架同一處（`utils/playerGuide.ts`）。
 *    紅線三條：**不得教拖放**（浮窗只認左右滑與下滑，`classifyLightboxSwipe`）；
 *    「開原圖」逐字對齊 `lightbox.openOriginal`；候選圖那條必須說清楚**整格
 *    點擊仍是循環挑選**（`cycleCandidate`，順序 discard→stage→album），放大另
 *    有一顆左上角的鍵——寫反了會讓玩家在挑圖時誤改去留。整章不談扣點：放大看
 *    圖不呼叫任何上游，也就沒有價籤可言。
 *  - 第 6 章 記憶與你：回憶錄＝`MemoirPage`（釘選／請角色忘記＝
 *    `memoir.knownFacts.*`、`memoir.timeline.*`），「角色眼中的你」＝
 *    `memoir.personaProjection.*`；聊天裡改稱呼＝
 *    `application/services/address_preference_observer_service.py` 的方向
 *    判讀（**這是全導覽最藏的一條，必寫**）；你的人設＝
 *    `PlayerPersonaNoteChip` /
 *    `playerPersonaNote.*`。
 *  - 第 7 章 創作專區：入口＝`stage.launchers.studio`；內容不在這裡重寫，
 *    章尾那顆按鈕直接開既有的 `studio/StudioGuideModal.vue`。
 *  - 第 8 章 帶到別的地方聊：自綁通道＝`ChannelSetupGuide` /
 *    `channelAccountNextStep.*`；主動訊息 fan-out＝proactive-scheduling；
 *    hosted 的官方 LINE 與三層階梯＝`backgroundDormancy.advisory` 與
 *    Cloud 帳號中心的通道頁（綁定從官方帳號裡的連結開始）。
 *  - 第 9 章 角色卡與備份：`.lumecard` ＝角色頁匯出／匯入；`.lumebackup`
 *    ＝`characterBackup.*`（密碼紅線逐字對齊
 *    `characterBackup.password.warning`；hosted 匯出節流＝
 *    `character_backup_export_service` 的 `_enforce_hosted_rate_limit`
 *    ——**以操作者計、不是每個角色各自算**的滾動 24 小時上限，只在
 *    `cloud_mode` 生效，超過直接擋下該次匯出）；現成角色卡＝
 *    `playerSidebar.characterCards.gallery.title`（**玩家面沒有「櫥窗」
 *    這個詞**，那是後台 feature key，且與 portal 的公開櫥窗頁撞名）。
 *  - 第 10 章 螢火與方案（hosted-only 整章）：`credits.*` 全套——
 *    `credits.price.*` 的價籤、`credits.badge.*` 的餘額、
 *    `credits.insufficient.*`、`credits.overage.*`。
 *
 * 價格一律只說「會扣點」、不寫死數字——數字屬於按鈕旁邊那顆即時讀價的
 * `ActionPriceHint`，凍進說明文案的數字是帳本隨時可以打破的承諾。第 10 章
 * 的價籤那條刻意**不是**全稱句：語音播放鍵按長度計價、旁邊沒有
 * `ActionPriceHint`，「凡扣款必有價籤」會在那顆按鈕上直接破功。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined } from '@ant-design/icons-vue'

import { UiButton } from '@/components/ui'
import { useAuth } from '@/composables/useAuth'
import {
  visiblePlayerGuideChapters,
  type PlayerGuideChapter,
} from '@/utils/playerGuide'
import PlayerGuideChapterSection from './PlayerGuideChapterSection.vue'

const props = defineProps<{ visible: boolean }>()

const emit = defineEmits<{ close: []; openStudioGuide: [] }>()

/**
 * Teleport needs a live `body` to aim at; there is none while rendering on
 * the server (or in the SSR-based component tests), where the dialog then
 * silently renders into nothing. Falling back to rendering in place keeps
 * the markup real in both worlds.
 */
const canTeleport = typeof document !== 'undefined'

const { t } = useI18n()
const { cloudMode } = useAuth()

/**
 * 句子級條件化：有 `SelfHost` 變體的句子一律走這裡挑鍵。多半是扣點——
 * 「按一次會扣點」在沒接雲端計費的自架站上是假的。
 */
function siteAware(key: string): string {
  return t(cloudMode.value ? key : `${key}SelfHost`)
}

interface RenderedChapter {
  key: string
  icon: string
  title: string
  lead: string
  items: string[]
  actionLabel?: string
}

const chapters = computed<RenderedChapter[]>(() =>
  visiblePlayerGuideChapters(cloudMode.value).map((chapter: PlayerGuideChapter) => {
    const base = `playerGuide.chapters.${chapter.key}`
    return {
      key: chapter.key,
      icon: chapter.icon,
      title: t(`${base}.title`),
      lead: t(`${base}.lead`),
      // 句子級 hard gate：`selfHostOnly` 的條在雲端整句不渲染。它與
      // `siteAware` 是兩個正交旗標——一個換句子、一個讓句子消失。
      items: chapter.items
        .filter((item) => !(item.selfHostOnly && cloudMode.value))
        .map((item) => {
          const key = `${base}.items.${item.id}`
          return item.siteAware ? siteAware(key) : t(key)
        }),
      // 借用創作區導覽自己的入口字串，不在 playerGuide 底下抄第二份標籤：
      // 兩顆按鈕開的是同一頁，名字理應一起改。
      actionLabel: chapter.opensStudioGuide
        ? t('studio.guide.openLabel')
        : undefined,
    }
  }),
)

/**
 * 創作專區那一章的按鈕：先關掉自己，再讓上層開創作區導覽。
 *
 * 疊第二層 modal 也做得到（兩者同一個 z-index，後掛上去的會蓋在上面），
 * 但兩份 modal 會同時聽 Esc——按一次兩份一起關，回不到玩家導覽。與其為
 * 了維持位置引進一套堆疊規則，不如換頁：導覽是查閱面，玩家本來就會重開。
 */
function requestStudioGuide() {
  emit('openStudioGuide')
  emit('close')
}

const activeKey = ref<string | null>(null)
const sectionEls = new Map<string, HTMLElement>()

function registerSection(key: string, el: unknown) {
  if (el instanceof HTMLElement) sectionEls.set(key, el)
  else sectionEls.delete(key)
}

function headingId(key: string): string {
  return `player-guide-chapter-${key}`
}

/**
 * 目錄是跳轉，不是分頁：全部章節都在同一條捲軸上，點目錄只是捲過去。
 * 分頁式會讓「我剛剛看到的那句在哪一章」變成一次搜尋，而導覽的用途就是
 * 回來查。
 */
function goToChapter(key: string) {
  activeKey.value = key
  sectionEls.get(key)?.scrollIntoView({ block: 'start', behavior: 'smooth' })
}

watch(() => props.visible, (visible) => {
  if (visible) {
    activeKey.value = chapters.value[0]?.key ?? null
    bindEscape()
  } else {
    unbindEscape()
  }
}, { immediate: true })

onBeforeUnmount(unbindEscape)

function bindEscape() {
  if (typeof window === 'undefined') return
  window.addEventListener('keydown', handleWindowKeydown)
}

function unbindEscape() {
  if (typeof window === 'undefined') return
  window.removeEventListener('keydown', handleWindowKeydown)
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (!props.visible) return
  if (event.key === 'Escape') emit('close')
}
</script>

<template>
  <Teleport to="body" :disabled="!canTeleport">
    <div
      v-if="visible"
      class="player-guide__backdrop"
      @click.self="emit('close')"
    >
      <section
        class="player-guide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="player-guide-title"
      >
        <header class="player-guide__header">
          <div class="player-guide__heading">
            <p class="spark-label">{{ t('playerGuide.eyebrow') }}</p>
            <h2 id="player-guide-title" class="display-title display-title--gradient">
              {{ t('playerGuide.title') }}
            </h2>
          </div>
          <UiButton
            variant="ghost"
            size="sm"
            :aria-label="t('common.actions.close')"
            @click="emit('close')"
          >
            <CloseOutlined aria-hidden="true" />
          </UiButton>
        </header>

        <p class="player-guide__intro">{{ t('playerGuide.intro') }}</p>

        <div class="player-guide__main">
          <nav class="player-guide__toc" :aria-label="t('playerGuide.navAria')">
            <UiButton
              v-for="chapter in chapters"
              :key="chapter.key"
              variant="chip"
              size="sm"
              class="player-guide__toc-item"
              :active="activeKey === chapter.key"
              @click="goToChapter(chapter.key)"
            >
              <span class="player-guide__toc-icon" aria-hidden="true">{{ chapter.icon }}</span>
              <span class="player-guide__toc-text">{{ chapter.title }}</span>
            </UiButton>
          </nav>

          <div class="player-guide__body">
            <div
              v-for="chapter in chapters"
              :key="chapter.key"
              :ref="el => registerSection(chapter.key, el)"
            >
              <PlayerGuideChapterSection
                :icon="chapter.icon"
                :title="chapter.title"
                :lead="chapter.lead"
                :items="chapter.items"
                :heading-id="headingId(chapter.key)"
                :action-label="chapter.actionLabel"
                @action="requestStudioGuide"
              />
            </div>
          </div>
        </div>

        <footer class="player-guide__footer">
          <UiButton variant="primary" size="sm" @click="emit('close')">
            {{ t('playerGuide.close') }}
          </UiButton>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.player-guide__backdrop {
  position: fixed;
  inset: 0;
  height: 100dvh;
  z-index: 920;
  display: flex;
  align-items: center;
  justify-content: center;
  padding:
    max(24px, var(--safe-area-top))
    max(16px, var(--safe-area-right))
    max(24px, var(--safe-area-bottom))
    max(16px, var(--safe-area-left));
  background: rgba(0, 0, 0, 0.62);
  backdrop-filter: blur(5px);
}

.player-guide {
  width: min(840px, calc(100vw - 32px));
  max-height: min(92dvh, 900px);
  display: flex;
  flex-direction: column;
  border-radius: 12px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.28);
  background:
    radial-gradient(620px 260px at 20% -60px, rgba(var(--color-primary-rgb), 0.22), transparent 70%),
    rgba(14, 10, 32, 0.96);
  color: var(--color-text);
  box-shadow: 0 24px 64px rgba(0, 0, 0, 0.46);
}

.player-guide__header {
  flex: 0 0 auto;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-4) var(--space-2);
}

.player-guide__heading {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.player-guide__heading h2 {
  margin: 0;
  font-size: 28px;
  letter-spacing: 0;
}

.player-guide__intro {
  flex: 0 0 auto;
  margin: 0;
  padding: 0 var(--space-4) var(--space-3);
  color: var(--color-text-secondary);
  line-height: 1.7;
}

/* 目錄與內文各自捲：九章的內文很長，目錄跟著捲走就等於沒有目錄。 */
.player-guide__main {
  flex: 1 1 auto;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 200px) minmax(0, 1fr);
  gap: var(--space-3);
  padding: 0 var(--space-4);
}

.player-guide__toc {
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-bottom: var(--space-2);
}

.player-guide__toc-item {
  justify-content: flex-start;
  text-align: left;
  white-space: normal;
}

.player-guide__toc-icon {
  margin-right: 6px;
  font-size: 11px;
  line-height: 1;
}

.player-guide__toc-text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.player-guide__body {
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  padding-bottom: var(--space-3);
}

.player-guide__footer {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  padding: var(--space-2) var(--space-4) var(--space-4);
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}

/* 窄螢幕：目錄摺到頂部橫向捲，內文吃掉整個寬度。 */
@media (max-width: 720px) {
  .player-guide__main {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto minmax(0, 1fr);
    gap: var(--space-2);
  }

  .player-guide__toc {
    flex-direction: row;
    overflow-x: auto;
    overflow-y: hidden;
    padding-bottom: 4px;
  }

  .player-guide__toc-item {
    flex: 0 0 auto;
    white-space: nowrap;
  }

  .player-guide__heading h2 {
    font-size: 24px;
  }

  .player-guide__header,
  .player-guide__intro,
  .player-guide__main,
  .player-guide__footer {
    padding-inline: var(--space-3);
  }
}
</style>
