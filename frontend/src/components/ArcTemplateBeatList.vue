<script setup lang="ts">
/**
 * Read-only beat-structure list for an arc template.
 *
 * Extracted from `ArcTemplatePicker.vue` (plan A1) so the picker and the
 * Creator Studio template preview render one shared beat markup instead
 * of two forks. Pure presentation — pass the beats of whichever template
 * view you want shown (translated or original).
 */
import { useI18n } from 'vue-i18n'
import type { ArcTemplateBeat } from '@/types/arcTemplate'

defineProps<{
  beats: ArcTemplateBeat[]
}>()

const { t } = useI18n()

function tensionLabel(tension: string): string {
  switch (tension) {
    case 'setup': return t('story.arcTemplatePicker.tension.setup')
    case 'rising': return t('story.arcTemplatePicker.tension.rising')
    case 'climax': return t('story.arcTemplatePicker.tension.climax')
    case 'falling': return t('story.arcTemplatePicker.tension.falling')
    case 'resolution': return t('story.arcTemplatePicker.tension.resolution')
    default: return tension
  }
}

function sceneTypeLabel(s: string): string {
  switch (s) {
    case 'encounter': return t('story.arcTemplatePicker.sceneType.encounter')
    case 'revelation': return t('story.arcTemplatePicker.sceneType.revelation')
    case 'conflict': return t('story.arcTemplatePicker.sceneType.conflict')
    case 'resolution': return t('story.arcTemplatePicker.sceneType.resolution')
    case 'interlude': return t('story.arcTemplatePicker.sceneType.interlude')
    default: return s
  }
}
</script>

<template>
  <div class="tpl-beats">
    <div
      v-for="beat in beats"
      :key="beat.sequence"
      :class="[
        'beat-card',
        `tension-${beat.tension}`,
        { optional: !beat.required },
      ]"
    >
      <div class="beat-head">
        <span class="beat-day">{{ t('story.arcTemplatePicker.beatDay', { day: beat.day_offset }) }}</span>
        <span class="beat-pill tension">{{ tensionLabel(beat.tension) }}</span>
        <span class="beat-pill scene">{{ sceneTypeLabel(beat.scene_type) }}</span>
        <span v-if="!beat.required" class="beat-pill optional">
          {{ t('story.arcTemplatePicker.optional') }}
        </span>
      </div>
      <div class="beat-title">{{ beat.title }}</div>
      <div class="beat-summary">{{ beat.summary }}</div>
      <div v-if="beat.location || beat.scene_characters.length > 0 || beat.dramatic_question" class="beat-scene">
        <div v-if="beat.location" class="scene-line">
          <span class="scene-label">{{ t('story.arcTemplatePicker.sceneLabels.location') }}</span>
          <span>{{ beat.location }}</span>
        </div>
        <div v-if="beat.scene_characters.length > 0" class="scene-line">
          <span class="scene-label">{{ t('story.arcTemplatePicker.sceneLabels.characters') }}</span>
          <span>{{ beat.scene_characters.join(t('common.listSeparator')) }}</span>
        </div>
        <div v-if="beat.dramatic_question" class="scene-line">
          <span class="scene-label">{{ t('story.arcTemplatePicker.sceneLabels.question') }}</span>
          <span>{{ beat.dramatic_question }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tpl-beats {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.beat-card {
  padding: 8px 10px;
  background: rgba(0, 0, 0, 0.18);
  border: 1px solid var(--color-border);
  border-left-width: 3px;
  border-radius: 4px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}

.beat-card.tension-setup { border-left-color: #6f9fbe; }
.beat-card.tension-rising { border-left-color: #d4a15a; }
.beat-card.tension-climax { border-left-color: #d06b6b; }
.beat-card.tension-falling { border-left-color: #8a7cb5; }
.beat-card.tension-resolution { border-left-color: #7ab28a; }
.beat-card.optional { opacity: 0.75; }

.beat-head {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  align-items: center;
}

.beat-day {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary-light);
  font-variant-numeric: tabular-nums;
}

.beat-pill {
  font-size: 9px;
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
}

.beat-pill.optional {
  background: rgba(180, 180, 180, 0.15);
  color: #b0b0b0;
}

.beat-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}

.beat-summary {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
  white-space: pre-wrap;
}

.beat-scene {
  margin-top: 4px;
  padding-top: 4px;
  border-top: 1px dashed var(--color-border);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.scene-line {
  font-size: 11px;
  color: var(--color-text-secondary);
  display: flex;
  gap: 6px;
}

.scene-label {
  flex-shrink: 0;
  width: 32px;
  color: var(--color-primary-light);
  font-weight: 600;
}
</style>
