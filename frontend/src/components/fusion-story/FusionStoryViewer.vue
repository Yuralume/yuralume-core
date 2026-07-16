<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type { FusionStory, FusionStoryBeat } from '@/types/fusionStory'
import {
  exportFusionStory,
  iterateFusionBeat,
  iterateFusionOutline,
  polishFusionStory,
  restoreFusionStoryVersion,
  type FusionStoryExportFormat,
} from '@/utils/api/fusionStory'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import FusionStoryExitHub from './FusionStoryExitHub.vue'
import FusionStoryShareCardModal from './FusionStoryShareCardModal.vue'
import FusionStoryStatusBadge from './FusionStoryStatusBadge.vue'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { formatDateTime } from '@/i18n/formatters'

const props = defineProps<{
  story: FusionStory
  characters: Character[]
  adaptingToArc?: boolean
  celebrate?: boolean
}>()

const emit = defineEmits<{
  (e: 'updated', story: FusionStory): void
  (e: 'error', message: string): void
  (e: 'adapt-requested'): void
  (e: 'continue-requested'): void
  (e: 'branch-requested'): void
}>()

const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()

const outlineHint = ref('')
const beatHint = ref('')
const beatActiveIndex = ref<number | null>(null)
const polishing = ref(false)
const regeneratingOutline = ref(false)
const regeneratingBeat = ref(false)

const characterById = computed(() => {
  const map: Record<string, Character> = {}
  for (const c of props.characters) map[c.id] = c
  return map
})

const cast = computed(() =>
  props.story.character_ids
    .map((id) => characterById.value[id]?.name || id)
    .join(t('common.listSeparator')),
)

const isBusy = computed(
  () => props.story.status !== 'ready' && props.story.status !== 'failed',
)

const progressPercent = computed(() => props.story.progress?.percent ?? null)

const progressLabel = computed(() => {
  const progress = props.story.progress
  if (!progress) return t(`fusionStory.status.${props.story.status}`)
  const stage = t(`fusionStory.status.${progress.stage}`)
  if (progress.stage === 'writing' && progress.beats_total > 0) {
    return `${stage} · ${t('fusionStory.viewer.progressBeats', {
      done: progress.beats_done,
      total: progress.beats_total,
    })}`
  }
  return stage
})

const totalChars = computed(() =>
  props.story.beats.reduce((sum, b) => sum + (b.actual_chars || 0), 0),
)

function actLabel(act: string): string {
  switch (act) {
    case 'opening':
      return t('fusionStory.acts.opening')
    case 'rising':
      return t('fusionStory.acts.rising')
    case 'turn':
      return t('fusionStory.acts.turn')
    case 'resolution':
      return t('fusionStory.acts.resolution')
    default:
      return act
  }
}

function focusNames(beat: FusionStoryBeat): string {
  if (!beat.focus_character_ids.length) return t('fusionStory.viewer.allCast')
  return beat.focus_character_ids
    .map((id) => characterById.value[id]?.name || id)
    .join(t('common.listSeparator'))
}

async function handleOutlineRegenerate() {
  if (isBusy.value || regeneratingOutline.value) return
  regeneratingOutline.value = true
  try {
    const next = await iterateFusionOutline(props.story.id, {
      hint: outlineHint.value.trim() || null,
    })
    outlineHint.value = ''
    emit('updated', next)
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('fusionStory.viewer.errors.regenerateOutlineFailed'))
  } finally {
    regeneratingOutline.value = false
  }
}

function startBeatRewrite(index: number) {
  if (isBusy.value) return
  beatActiveIndex.value = index
  beatHint.value = ''
}

function cancelBeatRewrite() {
  beatActiveIndex.value = null
  beatHint.value = ''
}

async function confirmBeatRewrite() {
  if (beatActiveIndex.value == null) return
  if (isBusy.value || regeneratingBeat.value) return
  const idx = beatActiveIndex.value
  regeneratingBeat.value = true
  try {
    const next = await iterateFusionBeat(props.story.id, {
      beat_index: idx,
      hint: beatHint.value.trim() || null,
    })
    beatActiveIndex.value = null
    beatHint.value = ''
    emit('updated', next)
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('fusionStory.viewer.errors.rewriteBeatFailed'))
  } finally {
    regeneratingBeat.value = false
  }
}

const confirmDialog = useConfirmDialog()

const shareCardOpen = ref(false)

const exportingFormat = ref<FusionStoryExportFormat | null>(null)

const previewVersion = ref<number | null>(null)
const restoringVersion = ref<number | null>(null)

function togglePreview(versionNumber: number) {
  previewVersion.value =
    previewVersion.value === versionNumber ? null : versionNumber
}

async function handleRestore(versionNumber: number) {
  if (isBusy.value || restoringVersion.value != null) return
  if (!await confirmDialog({
    content: t('fusionStory.viewer.versionsPanel.confirmRestore', {
      version: versionNumber,
    }),
    okText: t('fusionStory.viewer.versionsPanel.restore'),
  })) {
    return
  }
  restoringVersion.value = versionNumber
  try {
    const next = await restoreFusionStoryVersion(
      props.story.id, versionNumber,
    )
    previewVersion.value = null
    emit('updated', next)
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('fusionStory.viewer.errors.restoreFailed'))
  } finally {
    restoringVersion.value = null
  }
}

async function handleExport(format: FusionStoryExportFormat) {
  if (props.story.status !== 'ready' || exportingFormat.value) return
  exportingFormat.value = format
  try {
    const file = await exportFusionStory(props.story.id, format)
    const url = URL.createObjectURL(file.blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = file.filename
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('fusionStory.viewer.errors.exportFailed'))
  } finally {
    exportingFormat.value = null
  }
}

async function handlePolish() {
  if (isBusy.value || polishing.value) return
  polishing.value = true
  try {
    const next = await polishFusionStory(props.story.id)
    emit('updated', next)
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('fusionStory.viewer.errors.polishFailed'))
  } finally {
    polishing.value = false
  }
}
</script>

<template>
  <article class="viewer glass-panel">
    <header class="viewer__header">
      <div class="viewer__title-row">
        <h2 class="viewer__title display-title display-title--gradient">{{ story.title }}</h2>
        <FusionStoryStatusBadge :status="story.status" />
      </div>
      <div class="viewer__meta">
        <span>{{ t('fusionStory.viewer.cast', { cast }) }}</span>
        <span>{{ t('fusionStory.viewer.version', { version: story.head_version }) }}</span>
        <span>{{ t('fusionStory.viewer.length', { count: totalChars }) }}</span>
      </div>
      <p class="viewer__premise">{{ story.premise }}</p>
      <p v-if="story.error_message" class="viewer__error">
        {{ t('fusionStory.viewer.failureReason', { reason: story.error_message }) }}
      </p>
      <div v-if="isBusy" class="viewer__progress">
        <div class="viewer__progress-head">
          <span class="viewer__progress-label">{{ progressLabel }}</span>
          <span v-if="progressPercent != null" class="viewer__progress-pct">
            {{ progressPercent }}%
          </span>
        </div>
        <div class="viewer__progress-bar">
          <div
            class="viewer__progress-fill"
            :style="{ width: `${progressPercent ?? 0}%` }"
          />
        </div>
        <p class="viewer__progress-note">{{ t('fusionStory.viewer.progressNote') }}</p>
      </div>
    </header>

    <FusionStoryExitHub
      v-if="story.status === 'ready'"
      :celebrate="celebrate"
      :adapting-to-arc="adaptingToArc"
      :exporting-format="exportingFormat"
      @adapt="emit('adapt-requested')"
      @continue="emit('continue-requested')"
      @branch="emit('branch-requested')"
      @export="handleExport"
      @share="shareCardOpen = true"
    />

    <section class="viewer__section">
      <h3 class="viewer__section-title">{{ t('fusionStory.viewer.outlineTitle') }}</h3>
      <ul class="viewer__beats">
        <li
          v-for="(beat, idx) in story.beats"
          :key="beat.id"
          class="viewer__beat"
        >
          <div class="viewer__beat-head">
            <span class="viewer__beat-act">{{ actLabel(beat.act) }}</span>
            <span class="viewer__beat-title">{{ beat.title }}</span>
            <span class="viewer__beat-len">
              {{ t('fusionStory.viewer.beatLength', { actual: beat.actual_chars, target: beat.target_chars }) }}
            </span>
            <button
              class="viewer__beat-btn"
              :disabled="isBusy"
              @click="startBeatRewrite(idx)"
            >
              {{ t('fusionStory.viewer.rewrite') }}
            </button>
          </div>
          <div class="viewer__beat-hook">{{ beat.hook }}</div>
          <div class="viewer__beat-meta">
            <span>{{ t('fusionStory.viewer.dramaticQuestion', { value: beat.dramatic_question || t('common.fallback.none') }) }}</span>
            <span>{{ t('fusionStory.viewer.focus', { value: focusNames(beat) }) }}</span>
          </div>
          <template v-if="beat.content">
            <span v-if="isBusy" class="viewer__beat-draft">
              {{ t('fusionStory.viewer.draftBadge') }}
            </span>
            <pre class="viewer__beat-prose">{{ beat.content }}</pre>
          </template>
          <div
            v-if="beatActiveIndex === idx"
            class="viewer__beat-rewrite"
          >
            <textarea
              v-model="beatHint"
              class="field-textarea"
              rows="2"
              :placeholder="t('fusionStory.viewer.beatHintPlaceholder')"
            />
            <div class="viewer__beat-rewrite-actions">
              <button
                class="viewer__btn viewer__btn--primary"
                :disabled="regeneratingBeat || isBusy"
                @click="confirmBeatRewrite"
              >
                {{ regeneratingBeat ? t('fusionStory.viewer.rewriting') : t('fusionStory.viewer.submitRewrite') }}
              </button>
              <button class="viewer__btn" @click="cancelBeatRewrite">
                {{ t('common.actions.cancel') }}
              </button>
            </div>
          </div>
        </li>
      </ul>
    </section>

    <section class="viewer__section">
      <h3 class="viewer__section-title">
        {{ t('fusionStory.viewer.fullStoryTitle') }}
        <span
          v-if="story.status === 'ready' && story.full_text"
          class="viewer__final-badge"
        >
          {{ t('fusionStory.viewer.finalBadge') }}
        </span>
      </h3>
      <pre v-if="story.full_text" class="viewer__full">{{ story.full_text }}</pre>
      <p v-else class="viewer__empty">{{ t('fusionStory.viewer.fullStoryEmpty') }}</p>
    </section>

    <FusionStoryShareCardModal
      v-if="shareCardOpen"
      :story="story"
      :characters="characters"
      @close="shareCardOpen = false"
    />

    <section class="viewer__section viewer__actions">
      <h3 class="viewer__section-title">{{ t('fusionStory.viewer.iterationTitle') }}</h3>
      <div class="viewer__action-row">
        <textarea
          v-model="outlineHint"
          class="field-textarea"
          rows="2"
          :placeholder="t('fusionStory.viewer.outlineHintPlaceholder')"
        />
        <button
          class="viewer__btn viewer__btn--primary"
          :disabled="isBusy || regeneratingOutline"
          @click="handleOutlineRegenerate"
        >
          {{ regeneratingOutline ? t('fusionStory.viewer.regeneratingOutline') : t('fusionStory.viewer.regenerateOutline') }}
        </button>
      </div>
      <div class="viewer__action-row">
        <button
          class="viewer__btn"
          :disabled="isBusy || polishing"
          @click="handlePolish"
        >
          {{ polishing ? t('fusionStory.viewer.polishing') : t('fusionStory.viewer.polishOnly') }}
        </button>
      </div>
    </section>

    <section v-if="story.versions.length" class="viewer__section">
      <h3 class="viewer__section-title">
        {{ t('fusionStory.viewer.historyTitle', { count: story.versions.length }) }}
      </h3>
      <ul class="viewer__versions">
        <li
          v-for="v in story.versions"
          :key="v.id"
          class="viewer__version-row"
        >
          <div class="viewer__version-head">
            <span class="viewer__version-pill">v{{ v.version_number }}</span>
            <span class="viewer__version-label">{{ v.iteration_label }}</span>
            <span>{{ formatDateTime(v.created_at, locale, timeZone) }}</span>
            <span class="viewer__version-title">{{ v.title }}</span>
            <button
              v-if="v.full_text"
              class="viewer__btn viewer__btn--sm"
              @click="togglePreview(v.version_number)"
            >
              {{ previewVersion === v.version_number
                ? t('fusionStory.viewer.versionsPanel.hidePreview')
                : t('fusionStory.viewer.versionsPanel.preview') }}
            </button>
            <button
              v-if="v.full_text"
              class="viewer__btn viewer__btn--sm viewer__btn--primary"
              :disabled="isBusy || restoringVersion != null"
              @click="handleRestore(v.version_number)"
            >
              {{ restoringVersion === v.version_number
                ? t('fusionStory.viewer.versionsPanel.restoring')
                : t('fusionStory.viewer.versionsPanel.restore') }}
            </button>
          </div>
          <pre
            v-if="previewVersion === v.version_number"
            class="viewer__version-preview"
          >{{ v.full_text }}</pre>
        </li>
      </ul>
      <p class="viewer__version-hint">
        {{ t('fusionStory.viewer.versionsPanel.hint') }}
      </p>
    </section>
  </article>
</template>

<style scoped>
.viewer {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: var(--space-5);
  border-radius: 8px;
}
.viewer__header {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
  padding-bottom: 10px;
}
.viewer__title-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.viewer__title {
  font-size: 36px;
  margin: 0;
}
.viewer__meta {
  display: flex;
  gap: 14px;
  flex-wrap: wrap;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.65);
}
.viewer__premise {
  margin: 4px 0 0;
  color: rgba(255, 255, 255, 0.85);
  line-height: 1.75;
}
.viewer__error {
  color: var(--color-danger);
  margin: 4px 0 0;
}
.viewer__progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
  padding: 10px 12px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.28);
  border-radius: 6px;
  background: rgba(var(--color-primary-rgb), 0.08);
}
.viewer__progress-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}
.viewer__progress-label {
  font-size: 13px;
  color: var(--color-primary-light);
  animation: viewer-progress-pulse 1.6s ease-in-out infinite;
}
.viewer__progress-pct {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.7);
  font-variant-numeric: tabular-nums;
}
.viewer__progress-bar {
  height: 6px;
  border-radius: 999px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.08);
}
.viewer__progress-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(
    90deg,
    rgba(var(--color-primary-rgb), 0.65),
    var(--color-spark)
  );
  background-size: 200% 100%;
  transition: width 0.6s ease;
  animation: viewer-progress-shimmer 1.8s linear infinite;
}
.viewer__progress-note {
  margin: 0;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
}
@keyframes viewer-progress-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}
@keyframes viewer-progress-shimmer {
  0% { background-position: 0% 0; }
  100% { background-position: -200% 0; }
}
.viewer__beat-draft {
  display: inline-block;
  margin-top: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid rgba(255, 200, 120, 0.5);
  color: rgb(255, 208, 138);
  font-size: 11px;
}
.viewer__final-badge {
  display: inline-block;
  margin-left: 8px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid rgba(120, 220, 160, 0.5);
  color: rgb(140, 226, 176);
  font-size: 11px;
  letter-spacing: normal;
  text-transform: none;
}
.viewer__section {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.viewer__section-title {
  font-size: 14px;
  margin: 0;
  color: var(--color-spark);
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.viewer__beats {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.viewer__beat {
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 6px;
  padding: 10px;
  background: rgba(255, 255, 255, 0.04);
}
.viewer__beat-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.viewer__beat-act {
  background: rgba(var(--color-primary-rgb), 0.2);
  color: var(--color-primary-light);
  border-radius: 4px;
  padding: 2px 8px;
  font-size: 12px;
}
.viewer__beat-title {
  font-weight: 600;
  flex: 1;
}
.viewer__beat-len {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}
.viewer__beat-btn {
  background: transparent;
  color: var(--color-primary-light);
  border: 1px solid rgba(var(--color-primary-rgb), 0.5);
  border-radius: 4px;
  padding: 2px 8px;
  cursor: pointer;
  font-size: 12px;
}
.viewer__beat-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.viewer__beat-hook {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.75);
  margin: 4px 0;
}
.viewer__beat-meta {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.5);
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.viewer__beat-prose {
  white-space: pre-wrap;
  margin: 8px 0 0;
  padding: var(--space-3);
  background:
    repeating-linear-gradient(
      0deg,
      rgba(255, 255, 255, 0.018) 0 1px,
      transparent 1px 18px
    ),
    rgba(0, 0, 0, 0.22);
  border-radius: 4px;
  font-family: inherit;
  font-size: 14px;
  line-height: 1.9;
}
.viewer__beat-rewrite {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.viewer__beat-rewrite .field-textarea {
  width: 100%;
}
.viewer__beat-rewrite-actions {
  display: flex;
  gap: 8px;
}
.viewer__action-row {
  display: flex;
  gap: 8px;
  align-items: stretch;
}
.viewer__action-row .field-textarea {
  flex: 1;
}
.viewer__btn {
  background: rgba(255, 255, 255, 0.06);
  color: inherit;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 4px;
  padding: 6px 12px;
  cursor: pointer;
}
.viewer__btn--primary {
  background: rgba(var(--color-primary-rgb), 0.25);
  border-color: rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
}
.viewer__btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.viewer__btn--sm {
  padding: 3px 10px;
  font-size: 12px;
}
.viewer__full {
  white-space: pre-wrap;
  background:
    repeating-linear-gradient(
      0deg,
      rgba(255, 255, 255, 0.016) 0 1px,
      transparent 1px 20px
    ),
    rgba(0, 0, 0, 0.24);
  padding: var(--space-4);
  border-radius: 4px;
  margin: 0;
  font-family: inherit;
  line-height: 2;
  font-size: 15px;
}
.viewer__empty {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.5);
}
.viewer__versions {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  font-size: 12px;
  color: rgba(255, 255, 255, 0.65);
}
.viewer__version-row {
  padding: 6px 10px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.24);
  border-radius: 8px;
  background: rgba(var(--color-primary-rgb), 0.08);
}
.viewer__version-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
}
.viewer__version-preview {
  white-space: pre-wrap;
  margin: 8px 0 0;
  padding: var(--space-3);
  background: rgba(0, 0, 0, 0.24);
  border-radius: 4px;
  font-family: inherit;
  font-size: 13px;
  line-height: 1.8;
  max-height: 320px;
  overflow-y: auto;
}
.viewer__version-hint {
  margin: 4px 0 0;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.45);
}
.viewer__version-pill {
  color: var(--color-spark);
  font-weight: 700;
}
.viewer__version-label {
  color: var(--color-primary-light);
}
.viewer__version-title {
  color: rgba(255, 255, 255, 0.85);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

@media (max-width: 768px) {
  .viewer {
    padding: 12px;
    gap: 14px;
  }
  .viewer__title {
    font-size: 28px;
  }
  .viewer__meta {
    gap: 8px;
  }
  .viewer__beat {
    padding: 8px;
  }
  /* Per-beat header row: keep act badge + title on one row, push the
     length stat + rewrite button onto the next row so nothing clips. */
  .viewer__beat-head {
    gap: 6px;
  }
  .viewer__beat-title {
    flex-basis: calc(100% - 60px);
    order: 0;
  }
  .viewer__beat-act {
    order: -1;
  }
  .viewer__beat-len {
    order: 2;
    flex: 1;
  }
  .viewer__beat-btn {
    order: 3;
    padding: 6px 12px;
    font-size: 13px;
  }
  .viewer__beat-prose {
    font-size: 13px;
    padding: 10px;
    line-height: 1.8;
  }
  .viewer__action-row {
    flex-direction: column;
  }
  .viewer__action-row .field-textarea {
    width: 100%;
  }
  .viewer__btn {
    padding: 10px 14px;
    font-size: 14px;
  }
  .viewer__full {
    font-size: 14px;
    padding: 10px;
    line-height: 1.9;
  }
}
</style>
