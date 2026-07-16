<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { BookOutlined, CompassOutlined, EditOutlined } from '@ant-design/icons-vue'

import { UiButton } from '@/components/ui'
import type { Character } from '@/types/character'
import {
  isArcDiscoveryDismissed,
  rememberArcDiscoveryDismissed,
  shouldShowArcDiscovery,
} from '@/utils/arcDiscovery'

type ArcDiscoveryCharacter = Pick<Character, 'id' | 'name' | 'arc_template_id'> & {
  arc_series_id?: string | null
}

const props = defineProps<{
  character: ArcDiscoveryCharacter
  hasActiveArc: boolean
}>()

const emit = defineEmits<{
  startLlm: []
  pickTemplate: []
  openStudio: []
  dismiss: []
}>()

const { t } = useI18n()
const dismissed = ref(false)

const storage = computed(() => {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
})

function loadDismissedState() {
  dismissed.value = isArcDiscoveryDismissed(storage.value, props.character.id)
}

watch(() => props.character.id, loadDismissedState, { immediate: true })

const visible = computed(() => shouldShowArcDiscovery({
  character: props.character,
  hasActiveArc: props.hasActiveArc,
  dismissed: dismissed.value,
}))

function dismiss() {
  rememberArcDiscoveryDismissed(storage.value, props.character.id)
  dismissed.value = true
  emit('dismiss')
}
</script>

<template>
  <section
    v-if="visible"
    class="arc-discovery-card"
    :aria-label="t('story.arcDiscovery.ariaLabel')"
  >
    <p class="arc-discovery-card__kicker">{{ t('story.arcDiscovery.kicker') }}</p>
    <h3 class="arc-discovery-card__title">
      {{ t('story.arcDiscovery.title', { name: character.name }) }}
    </h3>
    <p class="arc-discovery-card__body">
      {{ t('story.arcDiscovery.body', { name: character.name }) }}
    </p>
    <div class="arc-discovery-card__actions">
      <UiButton
        variant="primary"
        size="sm"
        class="arc-discovery-card__action"
        @click="emit('startLlm')"
      >
        <CompassOutlined aria-hidden="true" />
        {{ t('story.arcDiscovery.actions.startLlm', { name: character.name }) }}
      </UiButton>
      <UiButton
        variant="secondary"
        size="sm"
        class="arc-discovery-card__action"
        @click="emit('pickTemplate')"
      >
        <BookOutlined aria-hidden="true" />
        {{ t('story.arcDiscovery.actions.pickTemplate') }}
      </UiButton>
      <UiButton
        variant="ghost"
        size="sm"
        class="arc-discovery-card__action"
        @click="emit('openStudio')"
      >
        <EditOutlined aria-hidden="true" />
        {{ t('story.arcDiscovery.actions.openStudio') }}
      </UiButton>
    </div>
    <UiButton
      variant="ghost"
      size="sm"
      class="arc-discovery-card__dismiss"
      @click="dismiss"
    >
      {{ t('story.arcDiscovery.actions.dismiss') }}
    </UiButton>
  </section>
</template>

<style scoped>
.arc-discovery-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px;
  border: 1px solid rgba(232, 155, 133, 0.26);
  border-radius: 8px;
  background:
    linear-gradient(135deg, rgba(183, 93, 63, 0.16), rgba(107, 153, 178, 0.1)),
    rgba(255, 255, 255, 0.025);
}

.arc-discovery-card__kicker {
  margin: 0;
  color: var(--color-primary-light);
  font-size: 11px;
  font-weight: 700;
  line-height: 1.2;
  text-transform: uppercase;
  letter-spacing: 0;
}

.arc-discovery-card__title {
  margin: 0;
  color: var(--color-text);
  font-size: 15px;
  font-weight: 700;
  line-height: 1.35;
  overflow-wrap: anywhere;
}

.arc-discovery-card__body {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.arc-discovery-card__actions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.arc-discovery-card__action {
  justify-content: center;
  gap: 8px;
  min-height: 34px;
}

.arc-discovery-card__dismiss {
  align-self: flex-start;
}
</style>
