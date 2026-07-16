<script setup lang="ts">
/**
 * 回憶錄主要內容（章節 + 三欄 layout）。
 *
 * 由 MemoirPage（直接路由 /memoir/:characterId）與 MemoirOverlay
 * （StagePage 上的 LumeGram 風 overlay）共用。本元件不負責頁面 chrome
 * （返回鈕、overlay backdrop），只渲染章節 header + 篩選 chip + 三欄主體。
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'

import MemoirChapterHeader from '@/components/memoir/MemoirChapterHeader.vue'
import MemoirFocusEntry from '@/components/memoir/MemoirFocusEntry.vue'
import MemoirKnownFactsPanel from '@/components/memoir/MemoirKnownFactsPanel.vue'
import MemoirMonthTimeline from '@/components/memoir/MemoirMonthTimeline.vue'
import MemoirRelatedList from '@/components/memoir/MemoirRelatedList.vue'
import PersonaProjectionPanel from '@/components/memoir/PersonaProjectionPanel.vue'
import { UiButton } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  getMemoirView,
  isPinLimitExceededError,
  pinMemoirEntry,
  unpinMemoirEntry,
  type MemoirEntry,
  type MemoirEntryKind,
  type MemoirView,
} from '@/utils/api/memoir'
import { deleteMemory } from '@/utils/api/memories'
import { clampSeedPrompt, composeMomentSeed } from '@/utils/fusionSeed'
import { stashStudioSeed } from '@/utils/studioSeedTransfer'

const props = defineProps<{
  characterId: string | null | undefined
}>()

const { t } = useI18n()
const router = useRouter()
const confirmDialog = useConfirmDialog()

type KindFilter = 'all' | MemoirEntryKind | 'pinned'

const view = ref<MemoirView | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const busyEntry = ref<string | null>(null)
const filter = ref<KindFilter>('all')
const focusKey = ref<string | null>(null)

const entryKey = (kind: MemoirEntryKind, id: string) => `${kind}:${id}`

const filteredTimeline = computed<MemoirEntry[]>(() => {
  if (!view.value) return []
  const all = view.value.timeline
  if (filter.value === 'all') return all
  if (filter.value === 'pinned') return all.filter(e => e.pinned)
  return all.filter(e => e.kind === filter.value)
})

const focusEntry = computed<MemoirEntry | null>(() => {
  if (!focusKey.value) return null
  return filteredTimeline.value.find(
    e => entryKey(e.kind, e.entry_id) === focusKey.value,
  ) ?? null
})

const focusPosition = computed(() => {
  const total = filteredTimeline.value.length
  if (!focusKey.value) return { current: 0, total }
  const idx = filteredTimeline.value.findIndex(
    e => entryKey(e.kind, e.entry_id) === focusKey.value,
  )
  return { current: idx < 0 ? 0 : idx + 1, total }
})

const filterChips = computed<Array<{ key: KindFilter, label: string }>>(() => [
  { key: 'all', label: t('memoir.filter.all') },
  { key: 'pinned', label: t('memoir.filter.pinned') },
  { key: 'memory', label: t('memoir.kind.memory') },
  { key: 'milestone', label: t('memoir.kind.milestone') },
  { key: 'emotion', label: t('memoir.kind.emotion') },
])

const pinSummary = computed(() => {
  if (!view.value) return ''
  return t('memoir.timeline.pinCount', {
    count: view.value.pin_count,
    limit: view.value.pin_limit,
  })
})

const autoFocusIfNeeded = () => {
  const list = filteredTimeline.value
  if (!list.length) {
    focusKey.value = null
    return
  }
  const stillValid = focusKey.value
    && list.some(e => entryKey(e.kind, e.entry_id) === focusKey.value)
  if (!stillValid) {
    focusKey.value = entryKey(list[0].kind, list[0].entry_id)
  }
}

const reload = async () => {
  if (!props.characterId) {
    view.value = null
    return
  }
  loading.value = true
  error.value = null
  try {
    view.value = await getMemoirView(props.characterId)
    autoFocusIfNeeded()
  }
  catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
  finally {
    loading.value = false
  }
}

onMounted(reload)
watch(() => props.characterId, reload)
watch(filter, autoFocusIfNeeded)

const selectEntry = (entry: MemoirEntry) => {
  focusKey.value = entryKey(entry.kind, entry.entry_id)
}

const stepFocus = (delta: 1 | -1) => {
  const list = filteredTimeline.value
  if (!list.length || !focusKey.value) return
  const idx = list.findIndex(
    e => entryKey(e.kind, e.entry_id) === focusKey.value,
  )
  const next = idx + delta
  if (next < 0 || next >= list.length) return
  focusKey.value = entryKey(list[next].kind, list[next].entry_id)
}

const handlePin = async (kind: MemoirEntryKind, id: string) => {
  if (!props.characterId) return
  busyEntry.value = entryKey(kind, id)
  try {
    await pinMemoirEntry(props.characterId, kind, id)
    await reload()
  }
  catch (err) {
    if (isPinLimitExceededError(err)) {
      const limit = err.response.data.detail.limit
      error.value = t('memoir.timeline.pinLimitReached', { limit })
    }
    else {
      error.value = err instanceof Error ? err.message : String(err)
    }
  }
  finally {
    busyEntry.value = null
  }
}

const handleUnpin = async (kind: MemoirEntryKind, id: string) => {
  if (!props.characterId) return
  busyEntry.value = entryKey(kind, id)
  try {
    await unpinMemoirEntry(props.characterId, kind, id)
    await reload()
  }
  catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
  finally {
    busyEntry.value = null
  }
}

const handlePinEntry = (entry: MemoirEntry) => handlePin(entry.kind, entry.entry_id)

const handleUnpinEntry = (entry: MemoirEntry) => handleUnpin(entry.kind, entry.entry_id)

const handleForgetMemory = async (entry: MemoirEntry) => {
  if (!props.characterId || (entry.kind !== 'memory' && entry.kind !== 'milestone')) {
    return
  }
  if (!await confirmDialog({
    content: t('memoir.knownFacts.confirmForget', {
      content: entry.summary.slice(0, 80),
    }),
    okText: t('memoir.knownFacts.forget'),
    danger: true,
  })) {
    return
  }
  busyEntry.value = entryKey(entry.kind, entry.entry_id)
  error.value = null
  try {
    if (entry.pinned) {
      await unpinMemoirEntry(props.characterId, entry.kind, entry.entry_id)
    }
    await deleteMemory(entry.entry_id)
    await reload()
  }
  catch (err) {
    error.value = err instanceof Error
      ? err.message
      : t('memoir.knownFacts.errors.forgetFailed')
  }
  finally {
    busyEntry.value = null
  }
}

const handleViewAll = () => {
  filter.value = 'all'
}

/**
 * "寫成番外": seed a fusion side-story from this memory. Carries the
 * memory summary as the moment + this character as the single cast; the
 * create form nudges for a second cast member (min 2). Works from both
 * hosts (the /memoir route page and the StagePage MemoirOverlay) since
 * router.push resolves against the app router either way. The seed goes
 * through the in-memory stash — memoir prose is private and must never
 * ride the URL (Referer / history / access logs).
 */
const handleWriteExtra = (entry: MemoirEntry) => {
  if (!props.characterId) return
  const seed = clampSeedPrompt(composeMomentSeed({
    momentText: entry.summary,
    strings: {
      momentLabel: t('memoir.writeExtra.momentLabel'),
      instructionLabel: t('memoir.writeExtra.instructionLabel'),
      instruction: t('memoir.writeExtra.instruction'),
    },
  }))
  stashStudioSeed({ seedPrompt: seed, cast: [props.characterId] })
  void router.push({ name: 'studio-fusion-stories' })
}

defineExpose({ reload })
</script>

<template>
  <div class="memoir-content">
    <p v-if="loading && !view" class="memoir-content__status">
      {{ t('memoir.loading') }}
    </p>

    <template v-else-if="!view">
      <div class="memoir-content__error-box">
        <p>{{ t('memoir.loadError') }}</p>
        <p v-if="error" class="memoir-content__error">{{ error }}</p>
        <UiButton variant="secondary" size="sm" @click="reload">
          {{ t('memoir.retry') }}
        </UiButton>
      </div>
    </template>

    <template v-else>
      <MemoirChapterHeader :chapters="view.chapters" />

      <div class="memoir-content__immersion-grid">
        <PersonaProjectionPanel
          :character-id="characterId"
          @corrected="reload"
        />
        <MemoirKnownFactsPanel
          :entries="view.timeline"
          :busy-key="busyEntry"
          @pin="handlePinEntry"
          @unpin="handleUnpinEntry"
          @forget="handleForgetMemory"
        />
      </div>

      <div class="memoir-content__toolbar">
        <div class="memoir-content__filters">
          <button
            v-for="chip in filterChips"
            :key="chip.key"
            type="button"
            class="memoir-content__chip"
            :class="{ 'memoir-content__chip--active': filter === chip.key }"
            @click="filter = chip.key"
          >
            {{ chip.label }}
          </button>
        </div>
        <span class="memoir-content__pin-count">{{ pinSummary }}</span>
      </div>

      <p v-if="error" class="memoir-content__error">{{ error }}</p>

      <div class="memoir-content__body">
        <MemoirMonthTimeline
          class="memoir-content__col memoir-content__col--left"
          :entries="filteredTimeline"
          :focus-key="focusKey"
          @select="selectEntry"
        />

        <section class="memoir-content__col memoir-content__col--center">
          <MemoirFocusEntry
            :entry="focusEntry"
            :position="focusPosition"
            :busy="!!focusEntry && busyEntry === entryKey(focusEntry.kind, focusEntry.entry_id)"
            @pin="handlePin"
            @unpin="handleUnpin"
            @prev="stepFocus(-1)"
            @next="stepFocus(1)"
            @write-extra="handleWriteExtra"
          />

          <footer class="memoir-content__footer">
            <div class="memoir-content__footer-art" aria-hidden="true" />
            <blockquote class="memoir-content__quote">
              {{ t('memoir.footer.quote') }}
              <cite class="memoir-content__quote-cite">— {{ t('memoir.footer.quoteAuthor') }}</cite>
            </blockquote>
          </footer>
        </section>

        <MemoirRelatedList
          class="memoir-content__col memoir-content__col--right"
          :entries="filteredTimeline"
          :focus-key="focusKey"
          @select="selectEntry"
          @view-all="handleViewAll"
        />
      </div>
    </template>
  </div>
</template>

<style scoped>
.memoir-content {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-height: 0;
}
.memoir-content__status {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.memoir-content__error {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-danger, #f87171);
}
.memoir-content__error-box {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-4);
  border-radius: 12px;
  background: rgba(248, 113, 113, 0.08);
  border: 1px solid rgba(248, 113, 113, 0.3);
  align-items: flex-start;
}
.memoir-content__immersion-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: var(--space-3);
  align-items: start;
}
.memoir-content__toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-2);
}
.memoir-content__filters {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.memoir-content__chip {
  appearance: none;
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  padding: 4px 12px;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}
.memoir-content__chip:hover {
  color: var(--color-text);
}
.memoir-content__chip--active {
  color: var(--color-text);
  background: rgba(139, 92, 246, 0.18);
  border-color: rgba(139, 92, 246, 0.45);
}
.memoir-content__pin-count {
  margin-left: auto;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  padding: 4px 10px;
  border-radius: 999px;
  background: rgba(139, 92, 246, 0.12);
  border: 1px solid rgba(139, 92, 246, 0.3);
}
.memoir-content__body {
  display: grid;
  grid-template-columns: 220px minmax(0, 1fr) 300px;
  gap: var(--space-4);
  align-items: start;
  min-height: 0;
}
.memoir-content__col {
  min-height: 0;
}
.memoir-content__col--left,
.memoir-content__col--right {
  max-height: 720px;
}
.memoir-content__col--center {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  align-items: stretch;
}
.memoir-content__footer {
  position: relative;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding-top: var(--space-4);
}
.memoir-content__footer-art {
  width: 280px;
  height: 120px;
  background-image: url('/memoir/book_footer.png');
  background-position: center;
  background-repeat: no-repeat;
  background-size: contain;
  filter: drop-shadow(0 0 18px rgba(139, 92, 246, 0.35));
}
.memoir-content__quote {
  position: relative;
  margin: 0;
  text-align: center;
  font-size: 14px;
  color: rgba(196, 181, 253, 0.88);
  font-family: 'Georgia', 'Noto Serif TC', 'Songti TC', serif;
  font-style: italic;
  line-height: 1.7;
  max-width: 520px;
  letter-spacing: 0.05em;
  padding: 0 28px;
}
.memoir-content__quote::before,
.memoir-content__quote::after {
  content: '';
  position: absolute;
  top: 50%;
  width: 24px;
  height: 1px;
  background: linear-gradient(
    90deg,
    transparent,
    rgba(196, 181, 253, 0.55),
    transparent
  );
}
.memoir-content__quote::before {
  left: -2px;
}
.memoir-content__quote::after {
  right: -2px;
}
.memoir-content__quote-cite {
  display: block;
  margin-top: 6px;
  font-style: normal;
  font-size: 11px;
  color: rgba(196, 181, 253, 0.7);
  letter-spacing: 0.18em;
  text-transform: uppercase;
}

@media (max-width: 1080px) {
  .memoir-content__immersion-grid {
    grid-template-columns: 1fr;
  }
  .memoir-content__body {
    grid-template-columns: 200px minmax(0, 1fr);
  }
  .memoir-content__col--right {
    grid-column: 1 / -1;
    max-height: none;
  }
}
@media (max-width: 720px) {
  .memoir-content__body {
    grid-template-columns: 1fr;
  }
  .memoir-content__col--left,
  .memoir-content__col--right {
    max-height: none;
  }
}
</style>
