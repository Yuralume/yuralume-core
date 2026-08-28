<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'
import ActionPriceHint from '@/components/ActionPriceHint.vue'
import { ACTION_CHAT } from '@/composables/useActionPricing'

type ChatGuideMode = 'stage' | 'dm'
type StarterKey = 'stageGreeting' | 'stageCurrent' | 'dmGreeting' | 'dmCheckIn'

const props = defineProps<{
  characterName: string
  mode: ChatGuideMode
  context: string
}>()

const emit = defineEmits<{
  selectStarter: [message: string]
  /**
   * 「不知道說什麼？讓{name}先開口」（plan TR4）——沒有 payload，是一次
   * 空白示意。呼叫端（`ChatPanel`）直接重用既有的 SN 送出路徑
   * （`handleStageNudgeSubmit('')`），這裡不重造任何送出邏輯。
   */
  requestNudge: []
}>()

const { t } = useI18n()

const starterKeys = computed<StarterKey[]>(() => (
  props.mode === 'dm'
    ? ['dmGreeting', 'dmCheckIn']
    : ['stageGreeting', 'stageCurrent']
))

// D-TR4-1（owner 拍板，2026-08-25）：同場限定，沿用 SN（示意）本身的同場
// 限定拍板。DM 側的「角色先開口」由 TR2 首聯（proactive）補位，不在這裡
// 重複做。
const showNudgeOption = computed(() => props.mode === 'stage')

const modeHint = computed(() => (
  props.mode === 'dm'
    ? t('chat.onboarding.dmHint', { name: props.characterName })
    : t('chat.onboarding.stageHint', { name: props.characterName })
))

const lifeHint = computed(() => t('chat.onboarding.lifeHint', {
  name: props.characterName,
}))

function starterText(key: StarterKey): string {
  return t(`chat.onboarding.starters.${key}`, { name: props.characterName })
}
</script>

<template>
  <section class="first-turn-guide" aria-labelledby="first-turn-guide-title">
    <div class="first-turn-guide__copy">
      <h3 id="first-turn-guide-title" class="first-turn-guide__title">
        {{ t('chat.onboarding.title', { name: characterName }) }}
      </h3>
      <p>{{ context }}</p>
      <p>{{ modeHint }}</p>
      <p>{{ lifeHint }}</p>
    </div>

    <!-- 視覺上與下面的 starter chips 並列但更突出——這是專為「完全不知道
         要說什麼」的玩家準備的出口，不必自己想開場白。 -->
    <div v-if="showNudgeOption" class="first-turn-guide__nudge">
      <UiButton
        variant="hero"
        size="sm"
        class="first-turn-guide__nudge-btn"
        @click="emit('requestNudge')"
      >
        <span class="first-turn-guide__nudge-glyph" aria-hidden="true">✦</span>
        {{ t('chat.onboarding.nudgeOption', { name: characterName }) }}
      </UiButton>
      <span class="first-turn-guide__nudge-price">
        <ActionPriceHint
          :action-key="ACTION_CHAT"
          tooltip-key="credits.price.chatTooltip"
          variant="chip"
        />
      </span>
    </div>

    <div class="first-turn-guide__starters" :aria-label="t('chat.onboarding.startersAria')">
      <UiButton
        v-for="key in starterKeys"
        :key="key"
        variant="chip"
        size="sm"
        class="first-turn-guide__starter"
        @click="emit('selectStarter', starterText(key))"
      >
        {{ starterText(key) }}
      </UiButton>
    </div>
  </section>
</template>

<style scoped>
.first-turn-guide {
  display: flex;
  width: min(100%, 520px);
  flex-direction: column;
  gap: 12px;
  align-self: center;
  margin: auto 0;
  padding: 16px;
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.035);
  text-align: left;
}

.first-turn-guide__copy {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.first-turn-guide__title {
  margin: 0;
  color: var(--color-text);
  font-size: 15px;
  font-weight: 700;
  line-height: 1.35;
}

.first-turn-guide__copy p {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.55;
}

.first-turn-guide__nudge {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

.first-turn-guide__nudge-btn {
  justify-content: flex-start;
  max-width: 100%;
  white-space: normal;
  text-align: left;
  line-height: 1.35;
}

.first-turn-guide__nudge-glyph {
  margin-right: 4px;
}

.first-turn-guide__nudge-price {
  min-width: 0;
}

.first-turn-guide__starters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.first-turn-guide__starter {
  justify-content: flex-start;
  max-width: 100%;
  white-space: normal;
  text-align: left;
  line-height: 1.35;
}
</style>
