<script setup lang="ts">
import { computed, nextTick, ref, type ComponentPublicInstance } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton, UiBadge } from '@/components/ui'
import type { FusionStoryExportFormat } from '@/utils/api/fusionStory'
import {
  FUSION_TO_ARC_OPERATOR_MODES,
  type FusionToArcOperatorMode,
} from '@/types/fusionStory'
import {
  isExitHubCoachmarkDismissed,
  rememberExitHubCoachmarkDismissed,
} from '@/utils/arcDiscovery'
import { nextRovingRadioIndex } from '@/utils/radioGroupKeyboard'

/**
 * Completion-page exit hub. Rendered by the viewer once a fusion story is
 * `ready`, first-screen visible: every finished work is the entrance to
 * the next creation. Kept presentational — all side effects (export,
 * share, navigation) live in the parent; this component only surfaces the
 * exits and emits intent.
 */
const props = withDefaults(
  defineProps<{
    /** Only true for a story the user just watched finish this session. */
    celebrate?: boolean
    adaptingToArc?: boolean
    exportingFormat?: FusionStoryExportFormat | null
  }>(),
  {
    celebrate: false,
    adaptingToArc: false,
    exportingFormat: null,
  },
)

const emit = defineEmits<{
  (e: 'adapt', mode: FusionToArcOperatorMode): void
  (e: 'continue'): void
  (e: 'branch'): void
  (e: 'export', format: FusionStoryExportFormat): void
  (e: 'share'): void
}>()

const { t } = useI18n()

const exportFormats: FusionStoryExportFormat[] = ['markdown', 'txt', 'epub']

/**
 * Where the creator stands in the arc this story is about to become
 * (OP1-C / plan 拍板 #2). Pre-selected as "write me in" because that is
 * the whole point of turning prose into something you then live through
 * — but it is shown, not assumed: the other two are one click away and
 * the copy under the row says what each one will do. (The server's
 * default for a caller that sends nothing is the opposite, `unchanged`;
 * silence is not a choice, whereas this row is.)
 */
const playerMode = ref<FusionToArcOperatorMode>('write_in')

const playerModeLabels: Record<FusionToArcOperatorMode, string> = {
  write_in: 'playerModeWriteIn',
  observer: 'playerModeObserver',
  unchanged: 'playerModeUnchanged',
}
const playerModeHints: Record<FusionToArcOperatorMode, string> = {
  write_in: 'playerModeWriteInHint',
  observer: 'playerModeObserverHint',
  unchanged: 'playerModeUnchangedHint',
}

const playerModeHint = computed(
  () => t(`fusionStory.exitHub.${playerModeHints[playerMode.value]}`),
)

/**
 * Roving tabindex + arrow-key selection for the custom `role="radio"` chip
 * group above (ARIA APG radiogroup pattern — the native `<input
 * type="radio">` behaviour a screen reader announces is arrow keys move
 * focus *and* the selection together, wrapping past either end; Tab only
 * stops once on the group, at whichever option is selected). The branching
 * lives in a pure, unit-tested helper (`nextRovingRadioIndex`) — this
 * component only wires the DOM effects (select + move focus) it decides on.
 */
const modeOptionRefs = ref<ComponentPublicInstance[]>([])

function selectPlayerMode(mode: FusionToArcOperatorMode) {
  playerMode.value = mode
}

async function focusPlayerModeOption(index: number) {
  await nextTick()
  const el = modeOptionRefs.value[index]?.$el as HTMLElement | undefined
  el?.focus()
}

function handlePlayerModeKeydown(event: KeyboardEvent) {
  const currentIndex = FUSION_TO_ARC_OPERATOR_MODES.indexOf(playerMode.value)
  const nextIndex = nextRovingRadioIndex(
    event.key,
    currentIndex,
    FUSION_TO_ARC_OPERATOR_MODES.length,
  )
  if (nextIndex === null) return
  event.preventDefault()
  selectPlayerMode(FUSION_TO_ARC_OPERATOR_MODES[nextIndex])
  void focusPlayerModeOption(nextIndex)
}

/** Guarded so SSR / privacy-mode never throws at setup. */
function getExitHubStorage(): Storage | null {
  if (typeof window === 'undefined') return null
  try {
    return window.localStorage
  } catch {
    return null
  }
}

const coachmarkDismissed = ref(
  isExitHubCoachmarkDismissed(getExitHubStorage()),
)
const showCoachmark = computed(() => !coachmarkDismissed.value)

function dismissCoachmark() {
  rememberExitHubCoachmarkDismissed(getExitHubStorage())
  coachmarkDismissed.value = true
}
</script>

<template>
  <section class="exit-hub" :class="{ 'is-celebrating': props.celebrate }">
    <div v-if="props.celebrate" class="exit-hub__celebrate" role="status">
      <span class="exit-hub__celebrate-glow" aria-hidden="true" />
      <div class="exit-hub__celebrate-copy">
        <UiBadge variant="success">{{ t('fusionStory.exitHub.celebrateTitle') }}</UiBadge>
        <p class="exit-hub__celebrate-body">
          {{ t('fusionStory.exitHub.celebrateBody') }}
        </p>
      </div>
    </div>

    <div class="exit-hub__head">
      <p class="spark-label">{{ t('fusionStory.exitHub.heading') }}</p>
    </div>

    <div
      v-if="showCoachmark"
      class="exit-hub__coachmark"
      role="note"
    >
      <span class="exit-hub__coachmark-body">
        {{ t('fusionStory.exitHub.coachmark') }}
      </span>
      <button
        type="button"
        class="exit-hub__coachmark-close"
        :aria-label="t('fusionStory.exitHub.coachmarkDismiss')"
        @click="dismissCoachmark"
      >
        ×
      </button>
    </div>

    <div
      class="exit-hub__mode"
      role="radiogroup"
      :aria-label="t('fusionStory.exitHub.playerModeLabel')"
      @keydown="handlePlayerModeKeydown"
    >
      <p class="exit-hub__mode-label">
        {{ t('fusionStory.exitHub.playerModeLabel') }}
      </p>
      <div class="exit-hub__mode-options">
        <UiButton
          v-for="mode in FUSION_TO_ARC_OPERATOR_MODES"
          :key="mode"
          ref="modeOptionRefs"
          class="exit-hub__mode-option"
          variant="chip"
          size="sm"
          role="radio"
          :aria-checked="playerMode === mode"
          :active="playerMode === mode"
          :tabindex="playerMode === mode ? 0 : -1"
          :disabled="props.adaptingToArc"
          @click="selectPlayerMode(mode)"
        >
          {{ t(`fusionStory.exitHub.${playerModeLabels[mode]}`) }}
        </UiButton>
      </div>
      <p class="exit-hub__mode-hint">{{ playerModeHint }}</p>
    </div>

    <div class="exit-hub__primary">
      <UiButton
        variant="hero"
        size="lg"
        block
        :loading="props.adaptingToArc"
        :disabled="props.adaptingToArc"
        @click="emit('adapt', playerMode)"
      >
        {{ props.adaptingToArc
          ? t('fusionStory.exitHub.adapting')
          : t('fusionStory.exitHub.adapt') }}
      </UiButton>
    </div>

    <div class="exit-hub__exits">
      <UiButton
        class="exit-hub__exit"
        variant="secondary"
        @click="emit('continue')"
      >
        {{ t('fusionStory.exitHub.continue') }}
      </UiButton>
      <UiButton
        class="exit-hub__exit"
        variant="secondary"
        @click="emit('branch')"
      >
        {{ t('fusionStory.exitHub.branch') }}
      </UiButton>
    </div>

    <div class="exit-hub__share">
      <span class="exit-hub__share-label">{{ t('fusionStory.exitHub.exportLabel') }}</span>
      <UiButton
        v-for="fmt in exportFormats"
        :key="fmt"
        variant="ghost"
        size="sm"
        :disabled="props.exportingFormat !== null"
        @click="emit('export', fmt)"
      >
        {{ props.exportingFormat === fmt
          ? t('fusionStory.viewer.exporting')
          : t(`fusionStory.viewer.exportFormats.${fmt}`) }}
      </UiButton>
      <UiButton
        variant="chip"
        size="sm"
        @click="emit('share')"
      >
        {{ t('fusionStory.shareCard.openButton') }}
      </UiButton>
    </div>
  </section>
</template>

<style scoped>
.exit-hub {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: var(--space-4);
  border-radius: 10px;
  border: 1px solid transparent;
  background:
    linear-gradient(rgba(18, 12, 42, 0.72), rgba(18, 12, 42, 0.72)) padding-box,
    linear-gradient(
      135deg,
      rgba(var(--color-spark-rgb), 0.5),
      rgba(var(--color-primary-rgb), 0.7),
      rgba(var(--color-secondary-rgb), 0.4)
    ) border-box;
}

.exit-hub__celebrate {
  position: relative;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 14px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid rgba(120, 220, 160, 0.42);
  background:
    linear-gradient(
      120deg,
      rgba(120, 220, 160, 0.16),
      rgba(var(--color-spark-rgb), 0.1) 60%,
      transparent
    ),
    rgba(0, 0, 0, 0.2);
  animation: exit-hub-rise 0.5s ease both;
}
.exit-hub__celebrate-glow {
  position: absolute;
  inset: 0 auto 0 -40%;
  width: 40%;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(255, 255, 255, 0.22),
    transparent
  );
  transform: translateX(-120%) skewX(-12deg);
  animation: exit-hub-sweep 1.1s ease 0.2s both;
  pointer-events: none;
}
.exit-hub__celebrate-copy {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.exit-hub__celebrate-body {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: rgba(255, 255, 255, 0.85);
}

.exit-hub__head {
  display: flex;
  align-items: center;
}

.exit-hub__coachmark {
  position: relative;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 34px 9px 12px;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.18);
  background: rgba(13, 23, 34, 0.9);
}
.exit-hub__coachmark-body {
  font-size: 12px;
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.82);
}
.exit-hub__coachmark-close {
  position: absolute;
  top: 6px;
  right: 6px;
  width: 24px;
  height: 24px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: rgba(255, 255, 255, 0.72);
  font: inherit;
  font-size: 17px;
  line-height: 24px;
  text-align: center;
  cursor: pointer;
}
.exit-hub__coachmark-close:hover {
  background: rgba(255, 255, 255, 0.1);
  color: #fff;
}

.exit-hub__mode {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.exit-hub__mode-label {
  margin: 0;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.72);
}
.exit-hub__mode-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.exit-hub__mode-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.55);
}

.exit-hub__primary {
  display: flex;
}
.exit-hub__exits {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
}
.exit-hub__exit {
  width: 100%;
}

.exit-hub__share {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.exit-hub__share-label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.6);
}

@keyframes exit-hub-rise {
  from {
    opacity: 0;
    transform: translateY(6px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
@keyframes exit-hub-sweep {
  to {
    transform: translateX(420%) skewX(-12deg);
  }
}

@media (max-width: 768px) {
  .exit-hub__exits {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .exit-hub__celebrate,
  .exit-hub__celebrate-glow {
    animation: none;
  }
  .exit-hub__celebrate-glow {
    display: none;
  }
}
</style>
