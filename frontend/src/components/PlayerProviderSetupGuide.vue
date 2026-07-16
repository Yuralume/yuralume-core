<script setup lang="ts">
import { useI18n } from 'vue-i18n'
import { SettingOutlined } from '@ant-design/icons-vue'
import { UiButton } from '@/components/ui'

const { t } = useI18n()

const emit = defineEmits<{
  start: []
}>()

const steps = ['enterAdmin', 'configure', 'routing'] as const
</script>

<template>
  <section class="provider-setup-guide" aria-labelledby="provider-setup-guide-title">
    <p class="provider-setup-guide__kicker">{{ t('playerSidebar.providerSetup.kicker') }}</p>
    <h2 id="provider-setup-guide-title" class="provider-setup-guide__title">
      {{ t('playerSidebar.providerSetup.title') }}
    </h2>
    <p class="provider-setup-guide__intro">
      {{ t('playerSidebar.providerSetup.intro') }}
    </p>

    <ol class="provider-setup-guide__steps">
      <li v-for="(step, index) in steps" :key="step" class="provider-setup-guide__step">
        <span class="provider-setup-guide__step-number">{{ index + 1 }}</span>
        <span class="provider-setup-guide__step-copy">
          <strong>{{ t(`playerSidebar.providerSetup.steps.${step}.title`) }}</strong>
          <span>{{ t(`playerSidebar.providerSetup.steps.${step}.hint`) }}</span>
        </span>
      </li>
    </ol>

    <UiButton
      variant="primary"
      block
      class="provider-setup-guide__action"
      @click="emit('start')"
    >
      <SettingOutlined aria-hidden="true" />
      {{ t('playerSidebar.providerSetup.action') }}
    </UiButton>
  </section>
</template>

<style scoped>
.provider-setup-guide {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
  border: 1px solid rgba(232, 155, 133, 0.4);
  border-radius: 8px;
  background:
    linear-gradient(135deg, rgba(214, 122, 58, 0.2), rgba(183, 93, 63, 0.12)),
    rgba(255, 255, 255, 0.025);
}

.provider-setup-guide__kicker {
  margin: 0;
  color: #f0a868;
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
  text-transform: uppercase;
  letter-spacing: 0;
}

.provider-setup-guide__title {
  margin: 0;
  color: var(--color-text);
  font-size: 16px;
  font-weight: 700;
  line-height: 1.35;
}

.provider-setup-guide__intro {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
}

.provider-setup-guide__steps {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 0;
  margin: 0;
  list-style: none;
}

.provider-setup-guide__step {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  min-width: 0;
}

.provider-setup-guide__step-number {
  display: inline-flex;
  width: 22px;
  height: 22px;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: rgba(232, 155, 133, 0.22);
  color: #f0a868;
  font-size: 12px;
  font-weight: 700;
}

.provider-setup-guide__step-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.45;
}

.provider-setup-guide__step-copy strong {
  color: var(--color-text);
  font-size: 13px;
  font-weight: 650;
}

.provider-setup-guide__action {
  margin-top: 2px;
  justify-content: center;
  gap: 8px;
}
</style>
