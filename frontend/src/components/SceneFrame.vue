<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'

/**
 * The scene frame — narration inside the chat thread (plan SC, ticket SC2).
 *
 * Chat bubbles are a conversation: they hug one side, they are capped at 90%
 * width, they are somebody speaking. A scene is the opposite of all three, and
 * the styling says so — full bleed, centred, framed, set in the display serif
 * the rest of the chat never uses. A player must be able to tell at a glance
 * that the story just took over, without reading a word.
 */
const props = defineProps<{
  /** The narration prose. Blank lines separate paragraphs. */
  text: string
  /** Scene heading. Absent for the closing narration and for old scenes. */
  title?: string | null
  location?: string | null
  mood?: string | null
  /** The send-off rather than the opening: same frame, quieter. */
  closing?: boolean
}>()

const { t } = useI18n()

const frameLabel = computed(() => (
  props.closing ? t('chat.storyScene.closingLabel') : t('chat.storyScene.label')
))

const paragraphs = computed<string[]>(() => (
  (props.text ?? '')
    .split(/\n+/)
    .map(part => part.trim())
    .filter(part => part.length > 0)
))

const hasHeading = computed(
  () => Boolean(props.title || props.location || props.mood),
)
</script>

<template>
  <section
    class="scene-frame"
    :class="{ 'scene-frame--closing': closing }"
    role="note"
    :aria-label="frameLabel"
  >
    <div class="scene-frame__banner">
      <span class="scene-frame__rule" aria-hidden="true" />
      <span class="scene-frame__label">{{ frameLabel }}</span>
      <span class="scene-frame__rule" aria-hidden="true" />
    </div>

    <header v-if="hasHeading" class="scene-frame__head">
      <h4 v-if="title" class="scene-frame__title">{{ title }}</h4>
      <dl v-if="location || mood" class="scene-frame__meta">
        <div v-if="location" class="scene-frame__meta-item">
          <dt>{{ t('chat.storyScene.meta.location') }}</dt>
          <dd>{{ location }}</dd>
        </div>
        <div v-if="mood" class="scene-frame__meta-item">
          <dt>{{ t('chat.storyScene.meta.mood') }}</dt>
          <dd>{{ mood }}</dd>
        </div>
      </dl>
    </header>

    <div class="scene-frame__body">
      <p v-for="(line, i) in paragraphs" :key="i">{{ line }}</p>
    </div>
  </section>
</template>

<style scoped>
.scene-frame {
  /* Full width: the one thing no chat bubble ever is. */
  align-self: stretch;
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 6px 0;
  padding: 14px 16px 16px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.34);
  border-radius: 10px;
  background:
    linear-gradient(
      180deg,
      rgba(var(--color-primary-rgb), 0.16),
      rgba(var(--color-secondary-rgb), 0.06)
    ),
    rgba(0, 0, 0, 0.24);
  box-shadow: 0 0 22px rgba(var(--color-primary-rgb), 0.12);
}

.scene-frame--closing {
  border-color: rgba(var(--color-primary-rgb), 0.2);
  background: rgba(0, 0, 0, 0.22);
  box-shadow: none;
}

.scene-frame__banner {
  display: flex;
  align-items: center;
  gap: 8px;
}

.scene-frame__rule {
  flex: 1;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(var(--color-primary-rgb), 0.45),
    transparent
  );
}

.scene-frame__label {
  flex: 0 0 auto;
  color: var(--color-primary-light);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.24em;
  text-transform: uppercase;
}

.scene-frame__head {
  display: flex;
  flex-direction: column;
  gap: 6px;
  align-items: center;
  text-align: center;
}

.scene-frame__title {
  margin: 0;
  color: var(--color-text);
  font-family: var(--font-display);
  font-size: 20px;
  font-weight: 700;
  line-height: 1.3;
}

.scene-frame__meta {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: 6px;
  margin: 0;
}

.scene-frame__meta-item {
  display: inline-flex;
  align-items: baseline;
  gap: 5px;
  padding: 2px 9px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.26);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.04);
}

.scene-frame__meta-item dt {
  color: var(--color-text-secondary);
  font-size: 10px;
  letter-spacing: 0.08em;
}

.scene-frame__meta-item dd {
  margin: 0;
  color: var(--color-text);
  font-size: 12px;
}

.scene-frame__body {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.scene-frame__body p {
  margin: 0;
  color: var(--color-text);
  font-size: 14px;
  line-height: 1.85;
  letter-spacing: 0.02em;
  text-align: justify;
}

.scene-frame--closing .scene-frame__body p {
  color: var(--color-text-secondary);
}

@media (max-width: 720px) {
  .scene-frame {
    padding: 12px 13px 14px;
  }

  .scene-frame__title {
    font-size: 17px;
  }

  .scene-frame__body p {
    font-size: 13px;
    line-height: 1.75;
  }
}
</style>
