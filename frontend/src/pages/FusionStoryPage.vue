<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { listCharacters } from '@/utils/api/characters'
import {
  clampSeedPrompt,
  composeContinuationSeed,
  parseCastQuery,
  serializeCastQuery,
} from '@/utils/fusionSeed'
import { stashStudioSeed, takeStudioSeed } from '@/utils/studioSeedTransfer'
import {
  adaptFusionStoryToArc,
  createFusionStory,
  deleteFusionStory,
  getFusionStory,
  listFusionStories,
} from '@/utils/api/fusionStory'
import {
  fetchFusionMaterialStats,
  type FusionMaterialStat,
} from '@/utils/api/studioJobs'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'
import type { Character } from '@/types/character'
import type {
  FusionStory,
  FusionStorySummary,
  FusionStoryStatus,
} from '@/types/fusionStory'
import { UiButton } from '@/components/ui'
import CharacterMultiSelect from '@/components/fusion-story/CharacterMultiSelect.vue'
import ArcTemplateIntakeWizard from '@/components/ArcTemplateIntakeWizard.vue'
import FusionStoryStatusBadge from '@/components/fusion-story/FusionStoryStatusBadge.vue'
import FusionStoryViewer from '@/components/fusion-story/FusionStoryViewer.vue'
import StudioCreatorPanel from '@/components/studio/StudioCreatorPanel.vue'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { formatDate } from '@/i18n/formatters'

const { t } = useI18n()
const confirmDialog = useConfirmDialog()
const route = useRoute()
const router = useRouter()
const { locale } = useLocale()
const { timeZone } = useTimezone()

const characters = ref<Character[]>([])
const materialStats = ref<Record<string, FusionMaterialStat>>({})
const stories = ref<FusionStorySummary[]>([])
const selectedStory = ref<FusionStory | null>(null)
const selectedCharacterIds = ref<string[]>([])
const promptText = ref('')
// The lone character carried in from a single-character seed (chat /
// memoir "寫成番外"). Drives a one-time, optional "invite a co-star" nudge
// (a solo cast is valid on its own since C1-5), which auto-clears the
// moment the user edits the cast (see seedCastHintName) — no watcher, no
// ordering bug against applySeedHandoff.
const seedSingleCastId = ref<string | null>(null)
const errorMessage = ref('')
const creating = ref(false)
const adaptingToArc = ref(false)
const adaptedDraft = ref<TemplateDraftPayload | null>(null)
const sidebarOpen = ref(false)
// True only for a story the user watched finish this session (see
// notifyIfFinished). Opening an existing finished story never celebrates.
const celebrate = ref(false)
const mainRef = ref<HTMLElement | null>(null)
let pollHandle: number | null = null

const inStudio = computed(() => route.matched.some(record => record.name === 'studio'))
const backTarget = computed(() => inStudio.value ? { name: 'studio-authoring' } : '/')

const isBusy = computed(() => {
  if (!selectedStory.value) return false
  const s = selectedStory.value.status
  return s !== 'ready' && s !== 'failed'
})

const characterById = computed(() => {
  const map: Record<string, Character> = {}
  for (const c of characters.value) map[c.id] = c
  return map
})

function coverImage(story: FusionStorySummary): string | null {
  for (const cid of story.character_ids) {
    const urls = characterById.value[cid]?.image_urls
    if (urls?.length) return urls[0]
  }
  return null
}

const canCreate = computed(() => {
  if (creating.value) return false
  // C1-5: a single selected character is enough to create a fusion story.
  if (selectedCharacterIds.value.length < 1) return false
  return promptText.value.trim().length > 0
})

// Selected characters whose fusion material is still thin — drives the
// soft, positive nudge below the picker. Never blocks creation.
const sparseSelectedNames = computed(() =>
  selectedCharacterIds.value
    .filter((id) => materialStats.value[id]?.tier === 'sparse')
    .map((id) => characterById.value[id]?.name)
    .filter((name): name is string => Boolean(name)),
)

// Name for the one-time optional "invite a co-star" nudge. Only shows
// while the seeded lone character is still the entire cast; adding/
// removing anyone (or a successful create resetting the cast) drops it
// automatically. The cast is already valid solo — this is an invitation,
// not a requirement.
const seedCastHintName = computed<string | null>(() => {
  const id = seedSingleCastId.value
  if (!id) return null
  if (
    selectedCharacterIds.value.length !== 1
    || selectedCharacterIds.value[0] !== id
  ) {
    return null
  }
  return characterById.value[id]?.name ?? null
})

/**
 * Load per-character richness badges. Fail-soft: badges are a hint, so a
 * stats error must never surface as a page error — just skip the badges.
 */
async function loadMaterialStats(ids: string[]) {
  if (!ids.length) {
    materialStats.value = {}
    return
  }
  try {
    materialStats.value = await fetchFusionMaterialStats(ids)
  } catch (err: unknown) {
    console.warn('fusion material stats load failed', err)
  }
}

async function refreshLists() {
  try {
    const [charList, storyList] = await Promise.all([
      listCharacters(),
      listFusionStories(),
    ])
    characters.value = charList
    stories.value = storyList
    void loadMaterialStats(charList.map((c) => c.id))
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('fusionStory.page.errors.loadListFailed')
  }
}

function notifyIfFinished(prevStatus: FusionStoryStatus, next: FusionStory) {
  const wasBusy = prevStatus !== 'ready' && prevStatus !== 'failed'
  if (!wasBusy || next.status === prevStatus) return
  if (next.status === 'ready') {
    // The user personally witnessed this story finish → light up the
    // completion celebration in the viewer's exit hub.
    celebrate.value = true
    notification.success({
      message: t('fusionStory.notifications.doneTitle'),
      description: next.title,
      duration: 4,
    })
  } else if (next.status === 'failed') {
    notification.error({
      message: t('fusionStory.notifications.failedTitle'),
      description: next.error_message || undefined,
      duration: 6,
    })
  }
}

async function refreshSelected(silent = false) {
  if (!selectedStory.value) return
  const prevStatus = selectedStory.value.status
  try {
    const next = await getFusionStory(selectedStory.value.id)
    notifyIfFinished(prevStatus, next)
    selectedStory.value = next
    // Sync the summary entry in the list too so status badge stays
    // accurate without forcing a full refresh.
    const idx = stories.value.findIndex((s) => s.id === next.id)
    if (idx >= 0) {
      stories.value[idx] = {
        id: next.id,
        character_ids: next.character_ids,
        title: next.title,
        premise: next.premise,
        status: next.status,
        head_version: next.head_version,
        error_message: next.error_message,
        progress: next.progress,
        total_chars: next.full_text
          ? next.full_text.length
          : next.beats.reduce((sum, b) => sum + (b.content?.length || 0), 0),
        created_at: next.created_at,
        updated_at: next.updated_at,
      }
    }
  } catch (err: unknown) {
    if (!silent) {
      errorMessage.value =
        err instanceof Error ? err.message : t('fusionStory.page.errors.updateFailed')
    }
  }
}

function startPolling() {
  stopPolling()
  pollHandle = window.setInterval(() => {
    if (selectedStory.value && isBusy.value) {
      void refreshSelected(true)
    }
  }, 1500)
}

function stopPolling() {
  if (pollHandle != null) {
    window.clearInterval(pollHandle)
    pollHandle = null
  }
}

async function selectStory(summary: FusionStorySummary) {
  errorMessage.value = ''
  sidebarOpen.value = false
  // Opening an existing work from the shelf is not a "just finished"
  // moment — never celebrate.
  celebrate.value = false
  try {
    selectedStory.value = await getFusionStory(summary.id)
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('fusionStory.page.errors.readFailed')
  }
}

function clearSelection() {
  selectedStory.value = null
  sidebarOpen.value = false
  celebrate.value = false
}

async function handleCreate() {
  if (!canCreate.value) return
  errorMessage.value = ''
  creating.value = true
  try {
    const created = await createFusionStory({
      character_ids: selectedCharacterIds.value,
      prompt: promptText.value.trim(),
    })
    selectedStory.value = created
    selectedCharacterIds.value = []
    promptText.value = ''
    await refreshLists()
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('fusionStory.page.errors.createFailed')
  } finally {
    creating.value = false
  }
}

async function handleDelete(summary: FusionStorySummary) {
  if (!await confirmDialog({
    content: t('fusionStory.page.confirmDelete', { title: summary.title }),
    okText: t('common.actions.delete'),
    danger: true,
  })) {
    return
  }
  try {
    await deleteFusionStory(summary.id)
    if (selectedStory.value?.id === summary.id) {
      selectedStory.value = null
    }
    await refreshLists()
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('fusionStory.page.errors.deleteFailed')
  }
}

async function handleAdaptToArc() {
  if (!selectedStory.value || selectedStory.value.status !== 'ready') return
  errorMessage.value = ''
  adaptingToArc.value = true
  try {
    adaptedDraft.value = await adaptFusionStoryToArc(selectedStory.value.id)
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('fusionStory.page.errors.adaptToArcFailed')
  } finally {
    adaptingToArc.value = false
  }
}

function handleAdaptedTemplateSaved() {
  const names = selectedStory.value
    ? selectedStory.value.character_ids
        .map((id) => characterById.value[id]?.name)
        .filter((name): name is string => Boolean(name))
    : []
  notification.success({
    message: names.length
      ? t('fusionStory.viewer.adaptSaved', {
          names: names.join(t('common.listSeparator')),
        })
      : t('fusionStory.viewer.adaptSavedFallback'),
    duration: 4,
  })
  adaptedDraft.value = null
}

/**
 * "續寫本篇": turn the finished story into a continuation seed and drop
 * the reader back into the create form with the same cast + a recap.
 */
async function handleContinueStory() {
  const story = selectedStory.value
  if (!story || story.status !== 'ready') return
  const seed = composeContinuationSeed({
    title: story.title,
    premise: story.premise,
    endingText: story.full_text,
    strings: {
      recapLabel: t('fusionStory.continuation.recapLabel'),
      endingLabel: t('fusionStory.continuation.endingLabel'),
      instructionLabel: t('fusionStory.continuation.instructionLabel'),
      instruction: t('fusionStory.continuation.instruction'),
    },
  })
  const ownedIds = characters.value.map((c) => c.id)
  promptText.value = clampSeedPrompt(seed)
  selectedCharacterIds.value = parseCastQuery(
    serializeCastQuery(story.character_ids),
    ownedIds,
  )
  celebrate.value = false
  selectedStory.value = null
  // Drop ?story= so a refresh doesn't re-open the finished work.
  void router.replace({ query: stripQueryKeys(['story']) })
  await nextTick()
  mainRef.value?.scrollTo({ top: 0, behavior: 'smooth' })
}

/**
 * "換個玩法 (開分歧劇場)": branch off the *same original premise*
 * (story.prompt, not the ending) into the branching-drama creator. The
 * prompt is user prose (often quoting private memories) so it travels
 * through the in-memory stash, never the URL.
 */
function handleBranchStory() {
  const story = selectedStory.value
  if (!story || story.status !== 'ready') return
  stashStudioSeed({
    seedPrompt: story.prompt,
    cast: [...story.character_ids],
  })
  void router.push({ name: 'studio-branching-dramas' })
}

/**
 * Shared entry seam (exit-hub continue/branch, chat & memoir "寫成番外",
 * studio seed templates): grab a pending seed handoff before ANY API
 * call fires. In-app entrances use the in-memory stash (private prose
 * never rides the URL); a `?seedPrompt=&cast=` query is kept as a
 * fallback for canned, non-sensitive deep links — its keys are stripped
 * here, synchronously, so no same-origin request ever carries them in
 * the Referer header.
 */
function captureSeedHandoff(): { seedPrompt: string; cast: string[] } | null {
  const stashed = takeStudioSeed()
  if (stashed) {
    return { seedPrompt: stashed.seedPrompt, cast: stashed.cast ?? [] }
  }
  const rawPrompt = route.query.seedPrompt
  const rawCast = route.query.cast
  const seedPrompt = typeof rawPrompt === 'string' ? rawPrompt : ''
  const rawCastStr = typeof rawCast === 'string' ? rawCast : ''
  if (!seedPrompt && !rawCastStr) return null
  void router.replace({ query: stripQueryKeys(['seedPrompt', 'cast']) })
  return {
    seedPrompt,
    cast: rawCastStr ? rawCastStr.split(',').map((s) => s.trim()) : [],
  }
}

/** Apply a captured handoff once the owned character list is loaded. */
function applySeedHandoff(handoff: { seedPrompt: string; cast: string[] }) {
  if (handoff.seedPrompt) {
    promptText.value = clampSeedPrompt(handoff.seedPrompt)
  }
  const ownedIds = characters.value.map((c) => c.id)
  const cast = parseCastQuery(serializeCastQuery(handoff.cast), ownedIds)
  if (cast.length) {
    selectedCharacterIds.value = cast
    // A single-character seed (chat / memoir entry) is already a valid
    // solo cast (C1-5) — arm the one-time, optional nudge inviting a
    // co-star, not a hard requirement to add one.
    seedSingleCastId.value = cast.length === 1 ? cast[0] : null
  }
}

/** Copy the current route query, dropping the given keys (string values
 *  only — mirrors what the create form can consume). */
function stripQueryKeys(keys: string[]): Record<string, string> {
  const next: Record<string, string> = {}
  for (const [key, value] of Object.entries(route.query)) {
    if (keys.includes(key)) continue
    if (typeof value === 'string') next[key] = value
  }
  return next
}

function statusOf(status: FusionStoryStatus): FusionStoryStatus {
  return status
}

onMounted(async () => {
  // Deep link from the completion web push (?story=<id>): open the
  // story directly so the notification lands on the finished work.
  const deepLinkId =
    typeof route.query.story === 'string' ? route.query.story : ''
  // Seed handoffs must be captured (and any query keys stripped) BEFORE
  // the first API call below — otherwise the seed text would ride the
  // Referer header of every same-origin request into access logs.
  const handoff = deepLinkId ? null : captureSeedHandoff()
  await refreshLists()
  if (deepLinkId) {
    // Landing on a pushed link is not a "just finished" moment.
    celebrate.value = false
    try {
      selectedStory.value = await getFusionStory(deepLinkId)
    } catch {
      // Story deleted since the push was sent — fall through to list.
    }
  } else if (handoff) {
    // Continue / branch / memory entry carrying a seed prompt + cast —
    // apply it now that the owned character list is loaded.
    applySeedHandoff(handoff)
  }
  startPolling()
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="fusion-page" :class="{ 'is-embedded': inStudio }">
    <header class="fusion-page__topbar">
      <div class="fusion-page__brand">
        <RouterLink :to="backTarget" class="fusion-page__back" :aria-label="t('fusionStory.page.back')">
          ←
          <span class="fusion-page__back-label">{{ t('fusionStory.page.back') }}</span>
        </RouterLink>
        <h1>{{ t('fusionStory.page.title') }}</h1>
        <button
          class="fusion-page__menu-btn"
          :aria-expanded="sidebarOpen"
          :aria-label="t('fusionStory.page.toggleHistory')"
          @click="sidebarOpen = !sidebarOpen"
        >
          {{ sidebarOpen ? '✕' : '☰' }}
          <span class="fusion-page__menu-count">{{ stories.length }}</span>
        </button>
      </div>
      <div class="fusion-page__hint">
        {{ t('fusionStory.page.hint') }}
      </div>
    </header>

    <div v-if="errorMessage" class="fusion-page__alert">
      {{ errorMessage }}
    </div>

    <div class="fusion-page__layout">
      <div
        v-if="sidebarOpen"
        class="fusion-page__scrim"
        @click="sidebarOpen = false"
      />
      <aside
        class="fusion-page__sidebar"
        :class="{ 'is-open': sidebarOpen }"
      >
        <div class="fusion-page__sidebar-head">
          <h2>{{ t('fusionStory.page.history') }}</h2>
          <UiButton size="sm" @click="clearSelection">
            {{ t('fusionStory.page.newStory') }}
          </UiButton>
        </div>
        <ul class="fusion-page__story-list">
          <li
            v-for="story in stories"
            :key="story.id"
            class="fusion-page__story"
            :class="{
              'is-selected': selectedStory?.id === story.id,
            }"
            :data-status="story.status"
          >
            <button class="fusion-page__story-btn" @click="selectStory(story)">
              <div class="fusion-page__story-cover">
                <img
                  v-if="coverImage(story)"
                  :src="coverImage(story)!"
                  alt=""
                  loading="lazy"
                />
                <span v-else class="fusion-page__story-cover-fallback">📖</span>
              </div>
              <div class="fusion-page__story-body">
                <div class="fusion-page__story-title">{{ story.title }}</div>
                <div class="fusion-page__story-meta">
                  <FusionStoryStatusBadge :status="statusOf(story.status)" />
                  <span>v{{ story.head_version }}</span>
                  <span
                    v-if="story.status !== 'ready' && story.status !== 'failed' && story.progress?.percent != null"
                    class="fusion-page__story-pct"
                  >
                    {{ story.progress.percent }}%
                  </span>
                </div>
                <div class="fusion-page__story-stats">
                  <span v-if="story.total_chars > 0">
                    {{ t('fusionStory.page.shelfChars', { count: story.total_chars }) }}
                  </span>
                  <span>{{ formatDate(story.updated_at, locale, timeZone) }}</span>
                </div>
                <div class="fusion-page__story-premise">
                  {{ story.premise }}
                </div>
              </div>
            </button>
            <button
              class="fusion-page__story-del"
              :title="t('common.actions.delete')"
              @click="handleDelete(story)"
            >
              ×
            </button>
          </li>
          <li v-if="!stories.length" class="fusion-page__empty">
            {{ t('fusionStory.page.emptyStories') }}
          </li>
        </ul>
      </aside>

      <main ref="mainRef" class="fusion-page__main">
        <FusionStoryViewer
          v-if="selectedStory"
          :story="selectedStory"
          :characters="characters"
          :adapting-to-arc="adaptingToArc"
          :celebrate="celebrate"
          @updated="(s) => (selectedStory = s)"
          @error="(m) => (errorMessage = m)"
          @adapt-requested="handleAdaptToArc"
          @continue-requested="handleContinueStory"
          @branch-requested="handleBranchStory"
        />
        <StudioCreatorPanel
          v-else
          class="fusion-page__creator"
          :eyebrow="t('studio.creatorPanel.eyebrow')"
          :title="t('fusionStory.page.createTitle')"
          :notice="t('fusionStory.page.notice')"
        >
          <div class="fusion-page__field">
            <label class="field-label">{{ t('fusionStory.page.castLabel') }}</label>
            <CharacterMultiSelect
              v-model="selectedCharacterIds"
              :characters="characters"
              :material-stats="materialStats"
              :min="1"
              :max="5"
            />
            <p
              v-if="seedCastHintName"
              class="field-hint fusion-page__seed-hint"
            >
              {{ t('fusionStory.page.castNeedMore', { name: seedCastHintName }) }}
            </p>
            <p
              v-if="sparseSelectedNames.length"
              class="field-hint fusion-page__material-hint"
            >
              {{ t('fusionStory.characterSelect.materialSoftHint', {
                names: sparseSelectedNames.join(t('common.listSeparator')),
              }) }}
            </p>
          </div>
          <div class="fusion-page__field">
            <label class="field-label">{{ t('fusionStory.page.promptLabel') }}</label>
            <textarea
              v-model="promptText"
              class="field-textarea"
              rows="4"
              :placeholder="t('fusionStory.page.promptPlaceholder')"
            />
          </div>
          <div class="fusion-page__actions">
            <UiButton
              variant="hero"
              :disabled="!canCreate"
              :loading="creating"
              @click="handleCreate"
            >
              {{ creating ? t('fusionStory.page.creating') : t('fusionStory.page.createAction') }}
            </UiButton>
          </div>
        </StudioCreatorPanel>
      </main>
    </div>
    <ArcTemplateIntakeWizard
      v-if="adaptedDraft"
      :initial-draft="adaptedDraft"
      @saved="handleAdaptedTemplateSaved"
      @close="adaptedDraft = null"
    />
  </div>
</template>

<style scoped>
.fusion-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  padding-top: calc(16px + var(--safe-area-top, 0px));
  padding-bottom: calc(16px + var(--safe-area-bottom, 0px));
  padding-left: calc(16px + var(--safe-area-left, 0px));
  padding-right: calc(16px + var(--safe-area-right, 0px));
  height: 100dvh;
  box-sizing: border-box;
  color: var(--color-text);
  background: var(--color-bg);
}

.fusion-page.is-embedded {
  height: auto;
  min-height: 0;
  padding: 0;
  background: transparent;
}

.fusion-page__topbar {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-border);
}
.fusion-page__brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.fusion-page__brand h1 {
  margin: 0;
  font-size: 20px;
  flex: 1;
  min-width: 0;
}
.fusion-page__back {
  color: rgba(255, 255, 255, 0.6);
  text-decoration: none;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.fusion-page__back:hover {
  color: rgba(255, 255, 255, 0.9);
}
.fusion-page__menu-btn {
  display: none;
  align-items: center;
  gap: 6px;
  background: rgba(255, 255, 255, 0.08);
  color: inherit;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 6px;
  padding: 6px 10px;
  cursor: pointer;
  font-size: 14px;
  line-height: 1;
}
.fusion-page__menu-count {
  font-size: 11px;
  background: rgba(var(--color-primary-rgb), 0.3);
  color: var(--color-primary-light);
  border-radius: 999px;
  padding: 1px 6px;
  min-width: 18px;
  text-align: center;
}
.fusion-page__hint {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
}
.fusion-page__alert {
  background: rgba(245, 34, 45, 0.15);
  border: 1px solid rgba(245, 34, 45, 0.5);
  color: var(--color-danger);
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
}
.fusion-page__layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 16px;
  flex: 1;
  min-height: 0;
  position: relative;
}
.fusion-page__scrim {
  display: none;
}
.fusion-page__sidebar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.03);
  padding: 12px;
  border-radius: 8px;
  min-height: 0;
}
.fusion-page__sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.fusion-page__sidebar-head h2 {
  font-size: 14px;
  margin: 0;
  color: rgba(255, 255, 255, 0.75);
}
.fusion-page__story-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fusion-page__story {
  --story-status-accent: var(--color-primary);
  display: flex;
  position: relative;
  border: 1px solid color-mix(in srgb, var(--story-status-accent) 22%, var(--color-border));
  border-radius: 8px;
  background:
    linear-gradient(90deg, color-mix(in srgb, var(--story-status-accent) 16%, transparent), transparent 28%),
    rgba(255, 255, 255, 0.03);
  overflow: hidden;
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.fusion-page__story::before {
  content: "";
  width: 4px;
  flex: 0 0 4px;
  background: var(--story-status-accent);
  box-shadow: 0 0 14px color-mix(in srgb, var(--story-status-accent) 38%, transparent);
}
.fusion-page__story[data-status='ready'] {
  --story-status-accent: var(--color-spark);
}
.fusion-page__story[data-status='failed'] {
  --story-status-accent: var(--color-danger);
}
.fusion-page__story:hover {
  transform: translateY(-1px);
  border-color: color-mix(in srgb, var(--story-status-accent) 52%, transparent);
}
.fusion-page__story.is-selected {
  border-color: transparent;
  background:
    linear-gradient(rgba(18, 12, 42, 0.9), rgba(18, 12, 42, 0.9)) padding-box,
    linear-gradient(135deg, var(--story-status-accent), var(--color-primary-light), var(--color-secondary)) border-box;
  box-shadow: 0 0 22px color-mix(in srgb, var(--story-status-accent) 24%, transparent);
}
.fusion-page__story-btn {
  flex: 1;
  text-align: left;
  background: transparent;
  border: 0;
  color: inherit;
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  flex-direction: row;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
}
.fusion-page__story-cover {
  flex: 0 0 44px;
  width: 44px;
  height: 58px;
  border-radius: 6px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid rgba(255, 255, 255, 0.1);
  display: flex;
  align-items: center;
  justify-content: center;
}
.fusion-page__story-cover img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.fusion-page__story-cover-fallback {
  font-size: 18px;
  opacity: 0.6;
}
.fusion-page__story-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  flex: 1;
}
.fusion-page__story-stats {
  display: flex;
  gap: 10px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.45);
  font-variant-numeric: tabular-nums;
}
.fusion-page__story-title {
  font-weight: 600;
  font-size: 14px;
}
.fusion-page__story-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.5);
}
.fusion-page__story-pct {
  color: var(--color-primary-light);
  font-variant-numeric: tabular-nums;
  animation: fusion-story-pct-pulse 1.6s ease-in-out infinite;
}
@keyframes fusion-story-pct-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.55; }
}
.fusion-page__story-premise {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.fusion-page__story-del {
  background: transparent;
  border: 0;
  color: rgba(255, 255, 255, 0.4);
  cursor: pointer;
  padding: 0 8px;
  font-size: 16px;
}
.fusion-page__story-del:hover {
  color: var(--color-danger);
}
.fusion-page__empty {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.45);
}
.fusion-page__main {
  overflow-y: auto;
}
.fusion-page__creator {
  align-self: start;
}
.fusion-page__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.fusion-page__field label {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
}
.fusion-page__field .field-textarea {
  width: 100%;
}
.fusion-page__material-hint {
  margin: 2px 0 0;
  line-height: 1.5;
}
.fusion-page__seed-hint {
  margin: 2px 0 0;
  line-height: 1.5;
  color: var(--color-primary-light);
}
.fusion-page__actions {
  display: flex;
  gap: 8px;
}
@media (max-width: 768px) {
  .fusion-page {
    gap: 8px;
    padding: 10px;
    padding-top: calc(10px + var(--safe-area-top, 0px));
    padding-bottom: calc(10px + var(--safe-area-bottom, 0px));
    padding-left: calc(10px + var(--safe-area-left, 0px));
    padding-right: calc(10px + var(--safe-area-right, 0px));
  }
  .fusion-page.is-embedded {
    padding: 0;
  }
  .fusion-page__brand h1 {
    font-size: 17px;
  }
  .fusion-page__back-label {
    display: none;
  }
  .fusion-page__menu-btn {
    display: inline-flex;
  }
  .fusion-page__hint {
    font-size: 11px;
    line-height: 1.5;
  }
  .fusion-page__layout {
    grid-template-columns: 1fr;
  }
  /* Sidebar slides in as a viewport-fixed drawer rather than a panel
     bound to the layout container — using ``position: fixed`` lets the
     scrim cover the *whole* viewport even when the main column is
     short, instead of being clipped to the layout container height. */
  .fusion-page__sidebar {
    position: fixed;
    top: 0;
    left: 0;
    width: min(82vw, 320px);
    height: 100vh;
    height: 100dvh;
    z-index: 45;
    transform: translateX(-110%);
    transition: transform 180ms ease;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
    background: var(--color-bg-secondary);
    padding-top: calc(12px + var(--safe-area-top, 0px));
    padding-bottom: calc(12px + var(--safe-area-bottom, 0px));
    padding-left: calc(12px + var(--safe-area-left, 0px));
  }
  .fusion-page__sidebar.is-open {
    transform: translateX(0);
  }
  .fusion-page__scrim {
    display: block;
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    height: 100dvh;
    background: rgba(0, 0, 0, 0.88);
    backdrop-filter: blur(2px);
    z-index: 35;
  }
  .fusion-page__actions {
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .fusion-page__story,
  .fusion-page__story:hover {
    transform: none;
    transition: none;
  }
}
</style>
