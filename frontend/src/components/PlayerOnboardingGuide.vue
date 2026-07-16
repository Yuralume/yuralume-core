<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { ImportOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { UiButton } from '@/components/ui'

const { t } = useI18n()

const emit = defineEmits<{
  create: []
  browseCards: []
}>()

const steps = ['create', 'talk', 'life'] as const
</script>

<template>
  <section class="player-onboarding" aria-labelledby="player-onboarding-title">
    <p class="player-onboarding__kicker">{{ t('playerSidebar.onboarding.kicker') }}</p>
    <h2 id="player-onboarding-title" class="player-onboarding__title">
      {{ t('playerSidebar.onboarding.title') }}
    </h2>
    <p class="player-onboarding__intro">
      {{ t('playerSidebar.onboarding.intro') }}
    </p>

    <ol class="player-onboarding__steps">
      <li v-for="(step, index) in steps" :key="step" class="player-onboarding__step">
        <span class="player-onboarding__step-number">{{ index + 1 }}</span>
        <span class="player-onboarding__step-copy">
          <strong>{{ t(`playerSidebar.onboarding.steps.${step}.title`) }}</strong>
          <span>{{ t(`playerSidebar.onboarding.steps.${step}.hint`) }}</span>
        </span>
      </li>
    </ol>

    <div class="player-onboarding__actions">
      <UiButton
        variant="primary"
        block
        class="player-onboarding__action"
        @click="emit('create')"
      >
        <PlusOutlined aria-hidden="true" />
        {{ t('playerSidebar.onboarding.createAction') }}
      </UiButton>
      <UiButton
        variant="secondary"
        block
        class="player-onboarding__action"
        @click="emit('browseCards')"
      >
        <ImportOutlined aria-hidden="true" />
        {{ t('playerSidebar.onboarding.officialCardsAction') }}
      </UiButton>
    </div>
    <p class="player-onboarding__after">
      {{ t('playerSidebar.onboarding.afterCreateHint') }}
    </p>
  </section>
</template>

<style scoped>
.player-onboarding {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  border: 1px solid rgba(232, 155, 133, 0.24);
  border-radius: 8px;
  background:
    linear-gradient(135deg, rgba(183, 93, 63, 0.14), rgba(107, 153, 178, 0.1)),
    rgba(255, 255, 255, 0.025);
}

.player-onboarding__kicker {
  margin: 0;
  color: var(--color-primary-light);
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
  text-transform: uppercase;
  letter-spacing: 0;
}

.player-onboarding__title {
  margin: 0;
  color: var(--color-text);
  font-size: 16px;
  font-weight: 700;
  line-height: 1.35;
}

.player-onboarding__intro,
.player-onboarding__after {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.player-onboarding__steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.player-onboarding__step {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  min-width: 0;
}

.player-onboarding__step-number {
  display: inline-flex;
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: rgba(232, 155, 133, 0.16);
  color: var(--color-primary-light);
  font-size: 12px;
  font-weight: 700;
}

.player-onboarding__step-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.45;
}

.player-onboarding__step-copy strong {
  color: var(--color-text);
  font-size: 13px;
  font-weight: 650;
}

.player-onboarding__actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.player-onboarding__action {
  margin-top: 2px;
  justify-content: center;
  gap: 8px;
}
</style>
