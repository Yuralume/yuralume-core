<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'

type ChatGuideMode = 'stage' | 'dm'
type StarterKey = 'stageGreeting' | 'stageCurrent' | 'dmGreeting' | 'dmCheckIn'

const props = defineProps<{
  characterName: string
  mode: ChatGuideMode
  stageBlocked?: boolean
  context: string
}>()

const emit = defineEmits<{
  selectStarter: [message: string]
}>()

const { t } = useI18n()

const starterKeys = computed<StarterKey[]>(() => (
  props.mode === 'dm' || props.stageBlocked
    ? ['dmGreeting', 'dmCheckIn']
    : ['stageGreeting', 'stageCurrent']
))

const modeHint = computed(() => (
  props.mode === 'dm' || props.stageBlocked
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
