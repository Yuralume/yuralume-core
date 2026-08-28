<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'

/**
 * 首輪一次性 tip：指向輸入列上「讓角色先開口」的圖示按鈕（plan TR4）。
 *
 * 純呈現——不知道按鈕在哪、不知道要不要顯示。顯示條件全部由呼叫端
 * （`ChatPanel`）透過 `utils/stageNudgeTip.ts` 的純函式算好、以 `visible`
 * 傳入；這裡只負責畫出來、以及在玩家關掉時往外回報一聲（記到
 * localStorage 是呼叫端的事，同 `ChatAssistDiscoveryHint` 的分工）。
 */
defineProps<{
  visible: boolean
  characterName: string
}>()

const emit = defineEmits<{
  dismiss: []
}>()

const { t } = useI18n()
</script>

<template>
  <Transition name="stage-nudge-tip">
    <div v-if="visible" class="stage-nudge-tip" role="note">
      <span class="stage-nudge-tip__glyph" aria-hidden="true">✦</span>
      <span class="stage-nudge-tip__text">
        {{ t('chat.stageNudge.tip', { name: characterName }) }}
      </span>
      <UiButton
        variant="ghost"
        size="sm"
        class="stage-nudge-tip__dismiss"
        :aria-label="t('chat.stageNudge.tipDismiss')"
        @click="emit('dismiss')"
      >
        {{ t('chat.stageNudge.tipDismiss') }}
      </UiButton>
    </div>
  </Transition>
</template>

<style scoped>
.stage-nudge-tip {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  padding: 7px 10px;
  border: 1px solid rgba(240, 185, 107, 0.32);
  border-radius: 8px;
  background: rgba(240, 185, 107, 0.08);
}

.stage-nudge-tip__glyph {
  flex: 0 0 auto;
  color: #f0b96b;
  font-size: 12px;
}

.stage-nudge-tip__text {
  min-width: 0;
  flex: 1 1 auto;
  color: var(--color-text);
  font-size: 12px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.stage-nudge-tip__dismiss {
  flex: 0 0 auto;
}

/*
 * 小箭頭，指向輸入列上的 StageNudgeControl 圖示按鈕（送出鈕左邊那顆）。
 *
 * 這顆 tip 跟 `.input-row` 是同一個 `.chat-input-area`（flex-direction:
 * column）底下的手足，兩者寬度、右邊界永遠對齊，所以可以直接用
 * `.stage-nudge-tip` 自己的 `right` 定位，不需要另外量 `.input-row`。
 *
 * `.input-row` 由右至左的手足與間距（全部是各自 CSS 規則裡的實際數字，
 * 不是量出來的猜測）：
 *   .send-btn      → min-width: 88px（見下方 `.send-btn`；三語系「送出」
 *                     /"Send"/"送信" 靜止態文字都撞這個下限，只有進入
 *                     sending 態文字變長才會超過，屆時箭頭會有些微偏差，
 *                     可接受）
 *   8px gap        → `.input-row { gap: 8px }`
 *   StageNudgeControl → 固定 44px（見 StageNudgeControl.vue
 *                     `.stage-nudge__trigger { width: 44px }`）
 * 箭頭對準圖示水平中心：8px + 88px + 44px/2 = 118px。
 */
.stage-nudge-tip::after {
  content: '';
  position: absolute;
  right: 118px;
  bottom: -6px;
  width: 10px;
  height: 10px;
  background: rgba(240, 185, 107, 0.08);
  border-right: 1px solid rgba(240, 185, 107, 0.32);
  border-bottom: 1px solid rgba(240, 185, 107, 0.32);
  transform: rotate(45deg);
}

.stage-nudge-tip-enter-active,
.stage-nudge-tip-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.stage-nudge-tip-enter-from,
.stage-nudge-tip-leave-to {
  opacity: 0;
  transform: translateY(4px);
}
</style>
