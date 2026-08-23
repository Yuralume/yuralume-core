<script setup lang="ts">
/**
 * 聊天 header 的「怎麼玩」常駐入口與它的一次性 coachmark（PG1）。
 *
 * 入口放在聊天 header 而不是側欄品牌區，是因為窄螢幕上側欄預設是收起的
 * 抽屜（`StagePage` 的 `sidebarOpen` 在 <900px 預設 false）——掛在裡面的
 * 提示對行動版玩家等於不存在。header 兩邊都看得到，而且 `header-right`
 * 已經有「你的人設」chip 這個同型常駐入口的先例，窄螢幕上那一列的狀態
 * 文字本來就會收起來，多一顆小按鈕不會擠掉任何控制項。
 *
 * coachmark 只出現到玩家開過一次導覽或自己關掉為止（localStorage，
 * user-wide，無 server 狀態），讀寫都 fail soft：隱私模式讀不到就照常
 * 顯示，寫不進去就下次再出現一次，總比整個畫面炸掉好。
 *
 * 創作專區那一章的按鈕開的是既有的「創作區怎麼玩」導覽，而它掛在這裡
 * 而不是玩家導覽內部——玩家導覽按下按鈕的同時會把自己關掉（避免兩份
 * modal 搶同一顆 Esc），關掉的元件沒辦法再負責開別人（PG2）。
 */
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { UiButton } from '@/components/ui'
import {
  isPlayerGuideCoachmarkDismissed,
  rememberPlayerGuideCoachmarkDismissed,
} from '@/utils/playerGuide'
import StudioGuideModal from '@/components/studio/StudioGuideModal.vue'
import PlayerGuideModal from './PlayerGuideModal.vue'

const { t } = useI18n()

const open = ref(false)
const studioGuideOpen = ref(false)
const coachmarkVisible = ref(false)

function storage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

onMounted(() => {
  coachmarkVisible.value = !isPlayerGuideCoachmarkDismissed(storage())
})

/** 開過導覽就等於這一課上完了——不需要再被提示一次。 */
function openGuide() {
  open.value = true
  dismissCoachmark()
}

function dismissCoachmark() {
  coachmarkVisible.value = false
  rememberPlayerGuideCoachmarkDismissed(storage())
}
</script>

<template>
  <div class="player-guide-entry">
    <UiButton
      variant="ghost"
      size="sm"
      class="player-guide-entry__button"
      :title="t('playerGuide.entry.hint')"
      :aria-label="t('playerGuide.entry.aria')"
      @click="openGuide"
    >
      <span class="player-guide-entry__icon" aria-hidden="true">?</span>
      <span>{{ t('playerGuide.entry.label') }}</span>
    </UiButton>

    <div v-if="coachmarkVisible" class="player-guide-entry__coachmark" role="note">
      <div class="player-guide-entry__coachmark-copy">
        <strong>{{ t('playerGuide.coachmark.title') }}</strong>
        <p>{{ t('playerGuide.coachmark.body') }}</p>
      </div>
      <button
        type="button"
        class="player-guide-entry__coachmark-close"
        :aria-label="t('playerGuide.coachmark.dismiss')"
        @click="dismissCoachmark"
      >×</button>
    </div>

    <PlayerGuideModal
      :visible="open"
      @close="open = false"
      @open-studio-guide="studioGuideOpen = true"
    />
    <StudioGuideModal
      :visible="studioGuideOpen"
      @close="studioGuideOpen = false"
    />
  </div>
</template>

<style scoped>
.player-guide-entry {
  position: relative;
  display: inline-flex;
}

.player-guide-entry__button {
  white-space: nowrap;
}

.player-guide-entry__icon {
  margin-right: 4px;
  font-size: 11px;
  line-height: 1;
  opacity: 0.75;
}

/* 氣泡掛在按鈕下方、向左對齊右緣，避免在窄螢幕上被 header 右邊界切掉。 */
.player-guide-entry__coachmark {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  z-index: 40;
  width: min(260px, calc(100vw - 32px));
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 10px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.4);
  background: rgba(14, 10, 32, 0.97);
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.42);
  text-align: left;
}

.player-guide-entry__coachmark-copy {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.player-guide-entry__coachmark-copy strong {
  font-size: 13px;
  color: var(--color-primary-light);
}

.player-guide-entry__coachmark-copy p {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.player-guide-entry__coachmark-close {
  flex: 0 0 auto;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
  font-size: 14px;
  line-height: 1;
  cursor: pointer;
}

.player-guide-entry__coachmark-close:hover {
  background: rgba(255, 255, 255, 0.16);
  color: var(--color-text);
}
</style>
