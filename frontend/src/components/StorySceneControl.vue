<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'

/**
 * The composer's scene control (plan SC, ticket SC2).
 *
 * One strip with two faces. Idle, it is the "Start a Scene" button plus the
 * promise that earns the press — *you do not have to think of a topic*. That
 * sentence is why the control sits next to the text box the player is stuck
 * in front of, rather than inside the `⋯` menu where a player who cannot
 * think of anything to say would never go looking.
 *
 * Open, the same strip becomes the scene's status line and the way out of it,
 * so a running scene is never something the player has to remember.
 *
 * The `price` slot is the mount point SC3-C fills with the hosted price chip;
 * this component stays free of any billing knowledge.
 */
const props = defineProps<{
  /** A scene is currently running. */
  sceneOpen: boolean
  /** Heading of the running scene, shown in the status strip. */
  sceneTitle?: string | null
  opening?: boolean
  ending?: boolean
  /** The composer cannot act right now (no character, a turn in flight). */
  disabled?: boolean
  /** This deployment offers no scenes — the button is withdrawn entirely. */
  unavailable?: boolean
  /** Already-localized refusal text, or null. */
  errorMessage?: string | null
  /**
   * Already-localized note about today's allowance ("2 scenes left today"),
   * or null when there is nothing certain to say — which is every self-host
   * render, so the node does not exist there at all.
   */
  quotaNote?: string | null
  /**
   * Today's allowance looked spent when the snapshot was read.
   *
   * Styling only — it warms the note's colour, it never disables the button.
   * The count is a rolling 24h window read once per session: it decays the
   * moment it is taken, and every refresh trigger lives behind this very
   * button, so a disable would lock itself shut past the point the server
   * would happily allow the press. The server owns the refusal; a stale
   * "exhausted" here costs one extra click, a stale disable costs the
   * player a scene they were entitled to.
   */
  quotaExhausted?: boolean
}>()

const emit = defineEmits<{
  open: []
  end: []
}>()

const { t } = useI18n()

/**
 * With no button and nothing to explain there is no strip at all — a trial
 * player is never shown a control they cannot use, nor a permanent apology
 * for its absence.
 */
const visible = computed(
  () => !props.unavailable || Boolean(props.errorMessage),
)

const openLabel = computed(() => (
  props.opening ? t('chat.storyScene.opening') : t('chat.storyScene.action')
))

const endLabel = computed(() => (
  props.ending ? t('chat.storyScene.ending') : t('chat.storyScene.end')
))
</script>

<template>
  <div v-if="visible" class="story-scene-control">
    <div v-if="!unavailable && sceneOpen" class="story-scene-control__status">
      <span class="story-scene-control__pulse" aria-hidden="true" />
      <span class="story-scene-control__status-text">
        <span class="story-scene-control__status-label">
          {{ t('chat.storyScene.inProgress') }}
        </span>
        <span v-if="sceneTitle" class="story-scene-control__status-title">
          {{ sceneTitle }}
        </span>
      </span>
      <UiButton
        variant="ghost"
        size="sm"
        class="story-scene-control__end"
        :loading="ending"
        :disabled="disabled"
        @click="emit('end')"
      >
        {{ endLabel }}
      </UiButton>
    </div>

    <div v-else-if="!unavailable" class="story-scene-control__idle">
      <UiButton
        variant="hero"
        size="sm"
        class="story-scene-control__start"
        :loading="opening"
        :disabled="disabled"
        @click="emit('open')"
      >
        <span class="story-scene-control__glyph" aria-hidden="true">✦</span>
        {{ openLabel }}
      </UiButton>
      <span class="story-scene-control__hint">
        {{ t('chat.storyScene.actionHint') }}
      </span>
      <span class="story-scene-control__price">
        <slot name="price" />
      </span>
    </div>

    <!-- Its own line rather than a fourth item in the idle row: on a narrow
         composer the row already wraps, and "N left today" belongs with the
         button it qualifies, not squeezed beside the price. Yields to the
         error line: after a 429 both would say "you are out for today" one
         above the other, and the error is the one that also promises
         nothing was charged. -->
    <p
      v-if="!unavailable && !sceneOpen && quotaNote && !errorMessage"
      :class="[
        'story-scene-control__quota',
        { 'story-scene-control__quota--exhausted': quotaExhausted },
      ]"
      role="status"
    >{{ quotaNote }}</p>

    <p v-if="errorMessage" class="story-scene-control__error" role="alert">
      {{ errorMessage }}
    </p>
  </div>
</template>

<style scoped>
.story-scene-control {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.story-scene-control__idle,
.story-scene-control__status {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.story-scene-control__status {
  padding: 6px 10px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.34);
  border-radius: 8px;
  background: rgba(var(--color-primary-rgb), 0.1);
}

.story-scene-control__pulse {
  flex: 0 0 auto;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--color-primary-light);
  box-shadow: 0 0 8px rgba(var(--color-primary-rgb), 0.8);
  animation: story-scene-pulse 1.8s ease-in-out infinite;
}

@keyframes story-scene-pulse {
  0%, 100% { opacity: 0.45; }
  50% { opacity: 1; }
}

.story-scene-control__status-text {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex: 1;
  min-width: 0;
}

.story-scene-control__status-label {
  flex: 0 0 auto;
  color: var(--color-primary-light);
  font-size: 11px;
  font-weight: 700;
}

.story-scene-control__status-title {
  min-width: 0;
  color: var(--color-text);
  font-family: var(--font-display);
  font-size: 13px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.story-scene-control__end {
  flex: 0 0 auto;
}

.story-scene-control__start {
  flex: 0 0 auto;
  gap: 6px;
}

.story-scene-control__glyph {
  font-size: 11px;
  line-height: 1;
}

.story-scene-control__hint {
  flex: 1;
  min-width: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.35;
}

.story-scene-control__price {
  flex: 0 0 auto;
}

.story-scene-control__error {
  margin: 0;
  color: #ff9a8a;
  font-size: 11px;
  line-height: 1.5;
}

/* An advisory, so it reads as secondary text — the warm tint is reserved
   for the state where the button is actually withheld. */
.story-scene-control__quota {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.5;
}

.story-scene-control__quota--exhausted {
  color: var(--color-spark);
}

@media (max-width: 720px) {
  /* The promise is the discovery affordance, so it survives the narrow
     layout on two lines rather than being truncated away. */
  .story-scene-control__idle {
    flex-wrap: wrap;
    gap: 6px 8px;
  }

  .story-scene-control__hint {
    flex: 1 0 100%;
  }
}
</style>
