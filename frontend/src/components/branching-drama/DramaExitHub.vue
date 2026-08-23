<script setup lang="ts">
import { computed, nextTick, ref, watch, type ComponentPublicInstance } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'
import ActionPriceHint from '@/components/ActionPriceHint.vue'
import { ACTION_BRANCHING_DRAMA_ADVANCE } from '@/composables/useActionPricing'
import {
  FUSION_TO_ARC_OPERATOR_MODES,
  type FusionToArcOperatorMode,
} from '@/types/fusionStory'
import DramaProgressTree from '@/components/branching-drama/DramaProgressTree.vue'
import type { DramaProgress } from '@/utils/dramaProgress'
import type { DramaTranscriptFormat } from '@/utils/dramaTranscript'
import { nextRovingRadioIndex } from '@/utils/radioGroupKeyboard'

/**
 * BD7 — the ending overlay's exits.
 *
 * A finished playthrough used to offer 「回列表」 and 「重玩」 and nothing
 * else: the line the player had just walked died with the overlay. This
 * turns the ending into a junction — walk it differently, keep it as a
 * script your characters can live through, take the text away with you, or
 * look at what this run added to the collection (BD9): the ending is the
 * moment the gallery means the most, and it used to be reachable only from
 * the drama's own page, which nobody goes back to mid-ovation.
 *
 * Presentational only: every side effect (starting a session, calling the
 * conversion, composing the file, opening the gallery) belongs to the
 * player / page above.
 */
const props = withDefaults(
  defineProps<{
    dramaTitle: string
    /**
     * 分歧圖與完成度 (D8.3) —— what the whole tree looks like and how
     * much of it these playthroughs have covered. Computed by the player
     * above from the sessions it already holds plus the one that just
     * ended; this view stays presentational. `null` while there is no
     * honest shape to draw.
     */
    progress?: DramaProgress | null
    /**
     * The run to burn gold in that graph —— here, always the path the
     * player has just finished walking. Which session that is is the
     * player's fact, not this component's guess.
     */
    currentSessionId?: string | null
    /** Pre-filled from how this drama was played (BD2 operator_position). */
    defaultMode?: FusionToArcOperatorMode
    adaptingToArc?: boolean
    exportingFormat?: DramaTranscriptFormat | null
    /**
     * A failure that happened *at* this ending (an export that could not be
     * composed). Shown here rather than in the player body behind the
     * overlay, where it would be invisible under the very screen that
     * triggered it.
     */
    errorMessage?: string | null
  }>(),
  {
    progress: null,
    currentSessionId: null,
    defaultMode: 'write_in',
    adaptingToArc: false,
    exportingFormat: null,
    errorMessage: null,
  },
)

const emit = defineEmits<{
  (e: 'replay'): void
  (e: 'adapt', mode: FusionToArcOperatorMode): void
  (e: 'export', format: DramaTranscriptFormat): void
  /** Show this drama's scene collection (BD9). Opened by the page above. */
  (e: 'gallery'): void
  (e: 'exit'): void
  (e: 'dismiss-error'): void
}>()

const { t } = useI18n()

const exportFormats: DramaTranscriptFormat[] = ['txt', 'md']

/**
 * Where the player stands in the arc this playthrough is about to become.
 * Starts on the framing the drama was actually played under — they already
 * answered this on the create form and the whole tree was planned against
 * it — but it stays a choice: someone who watched a drama may still want
 * to be written into the arc it becomes.
 */
const playerMode = ref<FusionToArcOperatorMode>(props.defaultMode)

watch(
  () => props.defaultMode,
  (mode) => { playerMode.value = mode },
)

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
  () => t(`branchingDrama.exitHub.${playerModeHints[playerMode.value]}`),
)

/**
 * Roving tabindex + arrow-key selection for the `role="radio"` chip group
 * (ARIA APG radiogroup pattern). The branching lives in the shared pure
 * helper; this component only wires the DOM effects it decides on.
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
</script>

<template>
  <section class="drama-exit">
    <h2 class="drama-exit__mark">~ End ~</h2>
    <p class="drama-exit__title">{{ props.dramaTitle }}</p>
    <p class="drama-exit__heading">{{ t('branchingDrama.exitHub.heading') }}</p>

    <!-- 你這輪點亮了哪一條（D8.3）。結局是這張圖最有感的一刻——剛走完
         的那條線燃金，先前幾輪的累積在它周圍亮著，其餘還是暗的。 -->
    <DramaProgressTree
      v-if="props.progress"
      class="drama-exit__tree"
      :progress="props.progress"
      :current-session-id="props.currentSessionId ?? null"
    />

    <UiButton
      class="drama-exit__replay"
      variant="hero"
      size="lg"
      block
      :disabled="props.adaptingToArc"
      @click="emit('replay')"
    >
      {{ t('branchingDrama.exitHub.replay') }}
    </UiButton>
    <!-- 換條路再走＝再開一場，開場那一段照推進計價，按之前就看得到。查不到
         價格（自架、按用量計費的方案）時整個節點不輸出——刻意不包一層容器，
         否則欄位排版的 gap 會在沒有價格時留下一道空隙。 -->
    <ActionPriceHint
      class="drama-exit__price"
      :action-key="ACTION_BRANCHING_DRAMA_ADVANCE"
      tooltip-key="credits.price.dramaStartTooltip"
      variant="chip"
    />
    <p class="drama-exit__hint">{{ t('branchingDrama.exitHub.replayHint') }}</p>

    <div
      class="drama-exit__mode"
      role="radiogroup"
      :aria-label="t('branchingDrama.exitHub.playerModeLabel')"
      @keydown="handlePlayerModeKeydown"
    >
      <p class="drama-exit__mode-label">
        {{ t('branchingDrama.exitHub.playerModeLabel') }}
      </p>
      <div class="drama-exit__mode-options">
        <UiButton
          v-for="mode in FUSION_TO_ARC_OPERATOR_MODES"
          :key="mode"
          ref="modeOptionRefs"
          variant="chip"
          size="sm"
          role="radio"
          :aria-checked="playerMode === mode"
          :active="playerMode === mode"
          :tabindex="playerMode === mode ? 0 : -1"
          :disabled="props.adaptingToArc"
          @click="selectPlayerMode(mode)"
        >
          {{ t(`branchingDrama.exitHub.${playerModeLabels[mode]}`) }}
        </UiButton>
      </div>
      <p class="drama-exit__mode-hint">{{ playerModeHint }}</p>
    </div>

    <UiButton
      variant="secondary"
      block
      :loading="props.adaptingToArc"
      :disabled="props.adaptingToArc"
      @click="emit('adapt', playerMode)"
    >
      {{ props.adaptingToArc
        ? t('branchingDrama.exitHub.adapting')
        : t('branchingDrama.exitHub.adapt') }}
    </UiButton>

    <div class="drama-exit__export">
      <span class="drama-exit__export-label">
        {{ t('branchingDrama.exitHub.exportLabel') }}
      </span>
      <UiButton
        v-for="fmt in exportFormats"
        :key="fmt"
        variant="ghost"
        size="sm"
        :disabled="props.exportingFormat !== null"
        @click="emit('export', fmt)"
      >
        {{ props.exportingFormat === fmt
          ? t('branchingDrama.exitHub.exporting')
          : t(`branchingDrama.exitHub.exportFormats.${fmt}`) }}
      </UiButton>
    </div>

    <!-- 圖集（BD9）：走過的場景留下來，沒走過的只剩剪影——結局是這件事最
         有感的時刻。免費，讀的是已經畫好的圖；按下去會離開結局畫面回到這
         齣戲的頁面，所以下面那句先講清楚要去哪。 -->
    <div class="drama-exit__gallery">
      <UiButton variant="secondary" block @click="emit('gallery')">
        {{ t('branchingDrama.exitHub.gallery') }}
      </UiButton>
      <p class="drama-exit__hint">
        {{ t('branchingDrama.exitHub.galleryHint') }}
      </p>
    </div>

    <p v-if="props.errorMessage" class="drama-exit__error" role="status">
      <span>{{ props.errorMessage }}</span>
      <UiButton variant="ghost" size="sm" @click="emit('dismiss-error')">
        {{ t('common.actions.close') }}
      </UiButton>
    </p>

    <UiButton variant="ghost" size="sm" @click="emit('exit')">
      {{ t('branchingDrama.player.backToList') }}
    </UiButton>
  </section>
</template>

<style scoped>
.drama-exit {
  display: flex;
  flex-direction: column;
  gap: 10px;
  width: min(440px, calc(100vw - 32px));
  max-height: calc(100dvh - 48px);
  overflow-y: auto;
  padding: 24px;
  border-radius: 12px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  background: rgba(10, 10, 20, 0.72);
  text-align: center;
}

.drama-exit__mark {
  margin: 0;
  font-size: 30px;
  font-weight: 300;
  letter-spacing: 0.15em;
  color: rgba(255, 255, 255, 0.9);
}
.drama-exit__title {
  margin: 0;
  font-size: 15px;
  color: rgba(255, 255, 255, 0.6);
}
.drama-exit__heading {
  margin: 4px 0 0;
  font-size: 12px;
  letter-spacing: 0.08em;
  color: rgba(255, 255, 255, 0.5);
}
.drama-exit__hint,
.drama-exit__mode-hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.55);
}

.drama-exit__price {
  align-self: center;
}

.drama-exit__tree {
  margin-top: 4px;
}

.drama-exit__mode {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 6px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
  text-align: left;
}
.drama-exit__mode-label {
  margin: 0;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.72);
}
.drama-exit__mode-options {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.drama-exit__export {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-top: 6px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}
.drama-exit__gallery {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 6px;
  border-top: 1px solid rgba(255, 255, 255, 0.1);
}

.drama-exit__error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 0;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(255, 255, 255, 0.16);
  background: rgba(0, 0, 0, 0.45);
  font-size: 12px;
  line-height: 1.5;
  color: rgba(255, 255, 255, 0.82);
  text-align: left;
}

.drama-exit__export-label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.6);
}

@media (max-width: 768px) {
  .drama-exit {
    padding: 18px 14px;
    gap: 8px;
  }
}
</style>
