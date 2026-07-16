<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { listCharacters } from '@/utils/api/characters'
import { clampSeedPrompt, parseCastQuery } from '@/utils/fusionSeed'
import { takeStudioSeed } from '@/utils/studioSeedTransfer'
import {
  createBranchingDrama,
  deleteBranchingDrama,
  getBranchingDrama,
  listBranchingDramas,
  listSessions,
} from '@/utils/api/branchingDrama'
import type { Character } from '@/types/character'
import type {
  BranchingDrama,
  BranchingDramaSummary,
  BranchingDramaStatus,
  DramaSession,
} from '@/types/branchingDrama'
import { UiButton } from '@/components/ui'
import CharacterMultiSelect from '@/components/fusion-story/CharacterMultiSelect.vue'
import BranchingDramaStatusBadge from '@/components/branching-drama/BranchingDramaStatusBadge.vue'
import BranchingDramaPlayer from '@/components/branching-drama/BranchingDramaPlayer.vue'
import StudioCreatorPanel from '@/components/studio/StudioCreatorPanel.vue'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { formatDateTime } from '@/i18n/formatters'
import { resolveSceneImageUrl } from '@/utils/sceneImage'

const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()
const route = useRoute()
const router = useRouter()

const characters = ref<Character[]>([])
const dramas = ref<BranchingDramaSummary[]>([])
const selectedDrama = ref<BranchingDrama | null>(null)
const selectedCharacterIds = ref<string[]>([])
const promptText = ref('')
const totalSegments = ref(6)
const errorMessage = ref('')
const creating = ref(false)
const sidebarOpen = ref(false)
const playing = ref(false)
const resumeSessionId = ref<string | null>(null)
const sessions = ref<DramaSession[]>([])
let pollHandle: number | null = null

const inStudio = computed(() => route.matched.some(record => record.name === 'studio'))
const backTarget = computed(() => inStudio.value ? { name: 'studio-authoring' } : '/')

const isBusy = computed(() => {
  if (!selectedDrama.value) return false
  const s = selectedDrama.value.status
  return s !== 'ready' && s !== 'failed'
})

const isReady = computed(
  () => selectedDrama.value?.status === 'ready',
)

const progressPercent = computed(() => {
  if (!selectedDrama.value || !isBusy.value) return 0
  const d = selectedDrama.value
  if (d.expected_node_count <= 0) return 0
  return Math.min(
    Math.round((d.generated_node_count / d.expected_node_count) * 100),
    99,
  )
})

const progressLabel = computed(() => {
  if (!selectedDrama.value) return ''
  const d = selectedDrama.value
  if (d.status === 'generating_outlines') {
    return t('branchingDrama.page.progressOutlines', {
      generated: d.generated_node_count,
      expected: d.expected_node_count,
    })
  }
  if (d.status === 'generating_images') {
    return t('branchingDrama.status.generatingImages')
  }
  return ''
})

const titleScreenImageUrl = computed(() => {
  if (!selectedDrama.value?.first_scene_image_path) return null
  return resolveSceneImageUrl(selectedDrama.value.first_scene_image_path)
})

const canCreate = computed(() => {
  if (creating.value) return false
  if (selectedCharacterIds.value.length < 2) return false
  return promptText.value.trim().length > 0
})

const segmentWarning = computed(() => {
  if (totalSegments.value < 9) return ''
  const count = (3 ** totalSegments.value - 1) / 2
  return t('branchingDrama.page.segmentWarning', {
    segments: totalSegments.value,
    count: Math.round(count),
  })
})

async function refreshLists() {
  try {
    const [charList, dramaList] = await Promise.all([
      listCharacters(),
      listBranchingDramas(),
    ])
    characters.value = charList
    dramas.value = dramaList
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('common.errors.loadFailed', { reason: t('common.errors.unknown') })
  }
}

async function refreshSelected(silent = false) {
  if (!selectedDrama.value) return
  try {
    const next = await getBranchingDrama(selectedDrama.value.id)
    selectedDrama.value = next
    const idx = dramas.value.findIndex((d) => d.id === next.id)
    if (idx >= 0) {
      dramas.value[idx] = {
        id: next.id,
        character_ids: next.character_ids,
        title: next.title,
        total_segments: next.total_segments,
        status: next.status,
        error_message: next.error_message,
        created_at: next.created_at,
        updated_at: next.updated_at,
      }
    }
  } catch (err: unknown) {
    if (!silent) {
      errorMessage.value =
        err instanceof Error ? err.message : t('branchingDrama.page.errors.updateFailed')
    }
  }
}

function startPolling() {
  stopPolling()
  pollHandle = window.setInterval(() => {
    if (selectedDrama.value && isBusy.value) {
      void refreshSelected(true)
    }
  }, 2000)
}

function stopPolling() {
  if (pollHandle != null) {
    window.clearInterval(pollHandle)
    pollHandle = null
  }
}

async function selectDrama(summary: BranchingDramaSummary) {
  errorMessage.value = ''
  sidebarOpen.value = false
  playing.value = false
  resumeSessionId.value = null
  try {
    const [drama, sessList] = await Promise.all([
      getBranchingDrama(summary.id),
      summary.status === 'ready'
        ? listSessions(summary.id)
        : Promise.resolve([]),
    ])
    selectedDrama.value = drama
    sessions.value = sessList
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('branchingDrama.page.errors.readFailed')
  }
}

function clearSelection() {
  selectedDrama.value = null
  sidebarOpen.value = false
  playing.value = false
  resumeSessionId.value = null
  sessions.value = []
}

async function handleCreate() {
  if (!canCreate.value) return
  errorMessage.value = ''
  creating.value = true
  try {
    const created = await createBranchingDrama({
      character_ids: selectedCharacterIds.value,
      prompt: promptText.value.trim(),
      total_segments: totalSegments.value,
    })
    selectedDrama.value = created
    selectedCharacterIds.value = []
    promptText.value = ''
    totalSegments.value = 6
    await refreshLists()
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('branchingDrama.page.errors.createFailed')
  } finally {
    creating.value = false
  }
}

async function handleDelete(summary: BranchingDramaSummary) {
  if (!await confirmDialog({
    content: t('branchingDrama.page.confirmDelete', { title: summary.title }),
    okText: t('common.actions.delete'),
    danger: true,
  })) {
    return
  }
  try {
    await deleteBranchingDrama(summary.id)
    if (selectedDrama.value?.id === summary.id) {
      selectedDrama.value = null
      playing.value = false
    }
    await refreshLists()
  } catch (err: unknown) {
    errorMessage.value =
      err instanceof Error ? err.message : t('branchingDrama.page.errors.deleteFailed')
  }
}

function startNewGame() {
  resumeSessionId.value = null
  playing.value = true
}

function resumeGame(sessionId: string) {
  resumeSessionId.value = sessionId
  playing.value = true
}

async function handleExitPlayer() {
  playing.value = false
  resumeSessionId.value = null
  if (selectedDrama.value) {
    try {
      sessions.value = await listSessions(selectedDrama.value.id)
    } catch { /* ignore */ }
  }
}

function formatSessionDate(iso: string): string {
  return formatDateTime(iso, locale.value, timeZone.value)
}

function statusOf(status: BranchingDramaStatus): BranchingDramaStatus {
  return status
}

function charNamesFor(ids: string[]): string {
  return ids
    .map((id) => characters.value.find((c) => c.id === id)?.name ?? '?')
    .join(t('common.listSeparator'))
}

/**
 * Shared entry seam (from a fusion story's "換個玩法" exit): grab a
 * pending seed handoff before ANY API call fires. In-app entrances use
 * the in-memory stash (the seed quotes user prose, which must never
 * ride the URL into Referer / history / access logs); a
 * `?seedPrompt=&cast=` query is kept as a fallback for canned deep
 * links — its keys are stripped here, synchronously, ahead of the first
 * same-origin request.
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
  const next: Record<string, string> = {}
  for (const [key, value] of Object.entries(route.query)) {
    if (key === 'seedPrompt' || key === 'cast') continue
    if (typeof value === 'string') next[key] = value
  }
  void router.replace({ query: next })
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
  const cast = parseCastQuery(handoff.cast.join(','), ownedIds)
  if (cast.length) selectedCharacterIds.value = cast
}

onMounted(async () => {
  // Capture (and strip) any seed handoff before the first API call so
  // seed text never rides the Referer header (see captureSeedHandoff).
  const handoff = captureSeedHandoff()
  await refreshLists()
  if (handoff) applySeedHandoff(handoff)
  startPolling()
})

onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="bd-page" :class="{ 'is-playing': playing, 'is-embedded': inStudio }">
    <header v-if="!playing" class="bd-page__topbar">
      <div class="bd-page__brand">
        <RouterLink :to="backTarget" class="bd-page__back" :aria-label="t('branchingDrama.page.back')">
          &larr;
          <span class="bd-page__back-label">{{ t('branchingDrama.page.back') }}</span>
        </RouterLink>
        <h1>{{ t('branchingDrama.page.title') }}</h1>
        <button
          class="bd-page__menu-btn"
          :aria-expanded="sidebarOpen"
          :aria-label="t('branchingDrama.page.toggleHistory')"
          @click="sidebarOpen = !sidebarOpen"
        >
          {{ sidebarOpen ? '✕' : '☰' }}
          <span class="bd-page__menu-count">{{ dramas.length }}</span>
        </button>
      </div>
      <div class="bd-page__hint">
        {{ t('branchingDrama.page.hint') }}
      </div>
    </header>

    <div v-if="errorMessage && !playing" class="bd-page__alert">
      {{ errorMessage }}
    </div>

    <!-- VN player mode -->
    <BranchingDramaPlayer
      v-if="playing && selectedDrama"
      :drama="selectedDrama"
      :characters="characters"
      :resume-session-id="resumeSessionId"
      @exit="handleExitPlayer"
      @error="(m) => { errorMessage = m; handleExitPlayer() }"
    />

    <!-- normal list + creator layout -->
    <div v-if="!playing" class="bd-page__layout">
      <div
        v-if="sidebarOpen"
        class="bd-page__scrim"
        @click="sidebarOpen = false"
      />
      <aside class="bd-page__sidebar" :class="{ 'is-open': sidebarOpen }">
        <div class="bd-page__sidebar-head">
          <h2>{{ t('branchingDrama.page.history') }}</h2>
          <UiButton size="sm" @click="clearSelection">
            {{ t('branchingDrama.page.newDrama') }}
          </UiButton>
        </div>
        <ul class="bd-page__drama-list">
          <li
            v-for="drama in dramas"
            :key="drama.id"
            class="bd-page__drama"
            :class="{ 'is-selected': selectedDrama?.id === drama.id }"
          >
            <button class="bd-page__drama-btn" @click="selectDrama(drama)">
              <div class="bd-page__drama-title">{{ drama.title }}</div>
              <div class="bd-page__drama-meta">
                <BranchingDramaStatusBadge :status="statusOf(drama.status)" />
                <span>{{ t('branchingDrama.page.segmentCountCompact', { count: drama.total_segments }) }}</span>
              </div>
              <div class="bd-page__drama-chars">
                {{ charNamesFor(drama.character_ids) }}
              </div>
            </button>
            <button
              class="bd-page__drama-del"
              :title="t('common.actions.delete')"
              @click="handleDelete(drama)"
            >
              &times;
            </button>
          </li>
          <li v-if="!dramas.length" class="bd-page__empty">
            {{ t('branchingDrama.page.emptyDramas') }}
          </li>
        </ul>
      </aside>

      <main class="bd-page__main">
        <!-- detail view for a selected drama -->
        <section
          v-if="selectedDrama"
          class="bd-page__detail"
          :class="{ 'is-ready': isReady }"
        >
          <div
            class="bd-page__title-screen"
            :class="{ 'has-scene-image': titleScreenImageUrl }"
            :style="titleScreenImageUrl ? { '--bd-title-image': `url(${titleScreenImageUrl})` } : undefined"
          >
            <div class="bd-page__title-copy">
              <p class="spark-label">{{ t('branchingDrama.page.titleScreenEyebrow') }}</p>
              <h2 class="display-title display-title--gradient">{{ selectedDrama.title }}</h2>
              <div class="bd-page__detail-meta">
                <BranchingDramaStatusBadge :status="statusOf(selectedDrama.status)" />
                <span>{{ t('branchingDrama.page.segmentCount', { count: selectedDrama.total_segments }) }}</span>
                <span>{{ t('branchingDrama.page.nodeCount', { count: selectedDrama.expected_node_count }) }}</span>
              </div>
            </div>
          </div>
          <p v-if="selectedDrama.warning" class="bd-page__warning">
            {{ selectedDrama.warning }}
          </p>
          <p v-if="selectedDrama.error_message" class="bd-page__error-detail">
            {{ selectedDrama.error_message }}
          </p>
          <div class="bd-page__detail-prompt">
            <label>{{ t('branchingDrama.page.promptLabel') }}</label>
            <div>{{ selectedDrama.prompt }}</div>
          </div>
          <div class="bd-page__detail-chars">
            <label>{{ t('branchingDrama.page.castLabel') }}</label>
            <div>{{ charNamesFor(selectedDrama.character_ids) }}</div>
          </div>
          <div v-if="isReady" class="bd-page__detail-actions">
            <UiButton class="bd-page__new-game" variant="hero" size="lg" @click="startNewGame">
              {{ t('branchingDrama.page.newGame') }}
            </UiButton>
          </div>

          <!-- session history -->
          <div v-if="isReady && sessions.length > 0" class="bd-page__sessions">
            <label>{{ t('branchingDrama.page.sessionsLabel') }}</label>
            <ul class="bd-page__session-list">
              <li
                v-for="(sess, index) in sessions"
                :key="sess.id"
                class="bd-page__session-item"
              >
                <button class="bd-page__session-btn" @click="resumeGame(sess.id)">
                  <span class="bd-page__session-slot">
                    {{ t('branchingDrama.page.sessionSlot', { index: sessions.length - index }) }}
                  </span>
                  <span class="bd-page__session-status" :data-status="sess.status">
                    {{ sess.status === 'playing' ? t('branchingDrama.page.sessionPlaying') : t('branchingDrama.page.sessionEnded') }}
                  </span>
                  <span class="bd-page__session-progress">
                    {{ t('branchingDrama.page.sessionProgress', { current: sess.turns.length, total: selectedDrama!.total_segments }) }}
                  </span>
                  <span class="bd-page__session-date">
                    {{ formatSessionDate(sess.updated_at) }}
                  </span>
                </button>
              </li>
            </ul>
          </div>

          <div v-if="isBusy" class="bd-page__progress">
            <div class="bd-page__progress-label">{{ progressLabel }}</div>
            <div class="bd-page__progress-bar">
              <div
                class="bd-page__progress-fill"
                :style="{ width: `${progressPercent}%` }"
              />
            </div>
            <div class="bd-page__progress-pct">{{ progressPercent }}%</div>
            <div class="bd-page__progress-note">
              {{ t('branchingDrama.page.progressNote') }}
            </div>
          </div>
        </section>

        <!-- creator form -->
        <StudioCreatorPanel
          v-else
          class="bd-page__creator"
          :eyebrow="t('studio.creatorPanel.eyebrow')"
          :title="t('branchingDrama.page.createTitle')"
          :notice="t('branchingDrama.page.notice')"
        >
          <div class="bd-page__field">
            <label class="field-label">{{ t('branchingDrama.page.castLabel') }}</label>
            <CharacterMultiSelect
              v-model="selectedCharacterIds"
              :characters="characters"
              :min="2"
              :max="5"
            />
          </div>
          <div class="bd-page__field">
            <label class="field-label">{{ t('branchingDrama.page.promptLabel') }}</label>
            <textarea
              v-model="promptText"
              class="field-textarea"
              rows="4"
              :placeholder="t('branchingDrama.page.promptPlaceholder')"
            />
          </div>
          <div class="bd-page__field bd-page__field--inline">
            <label class="field-label">{{ t('branchingDrama.page.totalSegmentsLabel') }}</label>
            <input
              v-model.number="totalSegments"
              type="number"
              class="field-input"
              :min="2"
              :max="15"
            />
          </div>
          <p v-if="segmentWarning" class="bd-page__warning">
            {{ segmentWarning }}
          </p>
          <div class="bd-page__actions">
            <UiButton
              variant="hero"
              :disabled="!canCreate"
              :loading="creating"
              @click="handleCreate"
            >
              {{ creating ? t('branchingDrama.page.creating') : t('branchingDrama.page.createAction') }}
            </UiButton>
          </div>
        </StudioCreatorPanel>
      </main>
    </div>
  </div>
</template>

<style scoped>
.bd-page {
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

.bd-page.is-embedded:not(.is-playing) {
  height: auto;
  min-height: 0;
  padding: 0;
  background: transparent;
}

.bd-page.is-playing {
  padding: 0;
  gap: 0;
}

.bd-page__topbar {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--color-border);
}
.bd-page__brand {
  display: flex;
  align-items: center;
  gap: 12px;
}
.bd-page__brand h1 {
  margin: 0;
  font-size: 20px;
  flex: 1;
  min-width: 0;
}
.bd-page__back {
  color: rgba(255, 255, 255, 0.6);
  text-decoration: none;
  font-size: 13px;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.bd-page__back:hover {
  color: rgba(255, 255, 255, 0.9);
}
.bd-page__menu-btn {
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
.bd-page__menu-count {
  font-size: 11px;
  background: rgba(var(--color-primary-rgb), 0.3);
  color: var(--color-primary-light);
  border-radius: 999px;
  padding: 1px 6px;
  min-width: 18px;
  text-align: center;
}
.bd-page__hint {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
}
.bd-page__alert {
  background: rgba(245, 34, 45, 0.15);
  border: 1px solid rgba(245, 34, 45, 0.5);
  color: var(--color-danger);
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
}

.bd-page__layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 16px;
  flex: 1;
  min-height: 0;
  position: relative;
}
.bd-page__scrim {
  display: none;
}

.bd-page__sidebar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  overflow-y: auto;
  background: rgba(255, 255, 255, 0.03);
  padding: 12px;
  border-radius: 8px;
  min-height: 0;
}
.bd-page__sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.bd-page__sidebar-head h2 {
  font-size: 14px;
  margin: 0;
  color: rgba(255, 255, 255, 0.75);
}

.bd-page__drama-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bd-page__drama {
  display: flex;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  overflow: hidden;
}
.bd-page__drama.is-selected {
  border-color: var(--color-primary);
  background: rgba(var(--color-primary-rgb), 0.08);
}
.bd-page__drama-btn {
  flex: 1;
  text-align: left;
  background: transparent;
  border: 0;
  color: inherit;
  padding: 8px 10px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}
.bd-page__drama-title {
  font-weight: 600;
  font-size: 14px;
}
.bd-page__drama-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: rgba(255, 255, 255, 0.5);
}
.bd-page__drama-chars {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.55);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bd-page__drama-del {
  background: transparent;
  border: 0;
  color: rgba(255, 255, 255, 0.4);
  cursor: pointer;
  padding: 0 8px;
  font-size: 16px;
}
.bd-page__drama-del:hover {
  color: var(--color-danger);
}
.bd-page__empty {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.45);
}

.bd-page__main {
  overflow-y: auto;
}

/* detail view */
.bd-page__detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 0;
  border: 1px solid rgba(var(--color-primary-rgb), 0.2);
  background: rgba(18, 12, 42, 0.42);
  border-radius: 8px;
  overflow: hidden;
}
.bd-page__title-screen {
  position: relative;
  min-height: 260px;
  padding: var(--space-5);
  display: flex;
  align-items: flex-end;
  background:
    linear-gradient(180deg, transparent 0%, rgba(10, 6, 24, 0.78) 72%, rgba(10, 6, 24, 0.96) 100%),
    radial-gradient(580px 260px at 28% 20%, rgba(var(--color-primary-rgb), 0.28), transparent 72%),
    radial-gradient(520px 220px at 80% 10%, rgba(var(--color-secondary-rgb), 0.18), transparent 72%),
    radial-gradient(circle, rgba(255, 255, 255, 0.22) 0 1px, transparent 1px),
    var(--color-bg-secondary);
  background-size: auto, auto, auto, 44px 44px, auto;
}
.bd-page__title-screen.has-scene-image {
  min-height: 320px;
  background:
    linear-gradient(180deg, rgba(8, 5, 20, 0.08) 0%, rgba(10, 6, 24, 0.36) 48%, rgba(10, 6, 24, 0.92) 100%),
    radial-gradient(520px 240px at 22% 14%, rgba(var(--color-primary-rgb), 0.28), transparent 70%),
    var(--bd-title-image);
  background-position: center, center, center;
  background-size: cover, cover, cover;
}
.bd-page__title-screen.has-scene-image::before {
  content: "";
  position: absolute;
  inset: 0;
  background:
    linear-gradient(90deg, rgba(10, 6, 24, 0.58) 0%, transparent 48%, rgba(10, 6, 24, 0.26) 100%),
    radial-gradient(460px 180px at 24% 80%, rgba(var(--color-spark-rgb), 0.16), transparent 74%);
  pointer-events: none;
}
.bd-page__title-screen.has-scene-image .bd-page__title-copy {
  position: relative;
  z-index: 1;
  max-width: 760px;
  text-shadow: 0 2px 22px rgba(0, 0, 0, 0.62);
}
.bd-page__title-copy {
  display: grid;
  gap: var(--space-2);
  min-width: 0;
}
.bd-page__title-copy h2,
.bd-page__title-copy p {
  margin: 0;
}
.bd-page__title-copy h2 {
  font-size: 42px;
}
.bd-page__detail-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 13px;
  color: rgba(255, 255, 255, 0.6);
}
.bd-page__detail > p,
.bd-page__detail-prompt,
.bd-page__detail-chars,
.bd-page__detail-actions,
.bd-page__sessions,
.bd-page__progress {
  margin-inline: var(--space-5);
}
.bd-page__detail-prompt,
.bd-page__detail-chars {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.bd-page__detail-prompt label,
.bd-page__detail-chars label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}
.bd-page__detail-prompt div,
.bd-page__detail-chars div {
  font-size: 14px;
  line-height: 1.6;
}
.bd-page__detail-actions {
  display: flex;
  gap: 10px;
  align-items: center;
  padding-top: 4px;
}
.bd-page__new-game {
  animation: bd-hero-glow 2.8s ease-in-out infinite;
}
.bd-page__progress {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: var(--space-5);
}
.bd-page__progress-label {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
  animation: pulse 1.5s infinite;
}
.bd-page__progress-bar {
  height: 6px;
  background: rgba(255, 255, 255, 0.08);
  border-radius: 3px;
  overflow: hidden;
}
.bd-page__progress-fill {
  position: relative;
  height: 100%;
  background: linear-gradient(90deg, var(--color-primary-dark), var(--color-primary-light));
  border-radius: 3px;
  transition: width 0.6s ease;
  min-width: 2px;
  overflow: hidden;
}
.bd-page__progress-fill::after {
  content: "";
  position: absolute;
  inset: 0;
  transform: translateX(-100%);
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.46), transparent);
  animation: bd-progress-shimmer 1.3s ease-in-out infinite;
}
.bd-page__progress-pct {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
  text-align: right;
}
.bd-page__progress-note {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.4);
}
@keyframes pulse {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}
@keyframes bd-hero-glow {
  0%, 100% {
    filter: brightness(1);
  }
  50% {
    filter: brightness(1.12);
  }
}
@keyframes bd-progress-shimmer {
  to {
    transform: translateX(100%);
  }
}
.bd-page__warning {
  font-size: 12px;
  color: #faad14;
  background: rgba(250, 173, 20, 0.1);
  border: 1px solid rgba(250, 173, 20, 0.3);
  padding: 6px 10px;
  border-radius: 4px;
  margin: 0;
}
.bd-page__error-detail {
  font-size: 12px;
  color: var(--color-danger);
  margin: 0;
}

/* creator form */
.bd-page__creator {
  align-self: start;
}
.bd-page__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bd-page__field--inline {
  flex-direction: row;
  align-items: center;
  gap: 10px;
}
.bd-page__field label {
  font-size: 13px;
  color: rgba(255, 255, 255, 0.7);
}
.bd-page__field .field-textarea {
  width: 100%;
}
.bd-page__field .field-input {
  width: 80px;
  text-align: center;
}
.bd-page__actions {
  display: flex;
  gap: 8px;
}
.bd-page__sessions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bd-page__sessions > label {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
}
.bd-page__session-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: var(--space-2);
}
.bd-page__session-item {
  border: 1px solid rgba(var(--color-primary-rgb), 0.18);
  border-radius: 8px;
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.1), rgba(255, 255, 255, 0.025)),
    rgba(18, 12, 42, 0.46);
  overflow: hidden;
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.bd-page__session-item:hover {
  transform: translateY(-1px);
  border-color: rgba(var(--color-spark-rgb), 0.42);
  box-shadow: 0 0 20px rgba(var(--color-primary-rgb), 0.18);
}
.bd-page__session-btn {
  width: 100%;
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: start;
  gap: 10px;
  padding: var(--space-3);
  background: transparent;
  border: 0;
  color: inherit;
  cursor: pointer;
  font-size: 13px;
  text-align: left;
}
.bd-page__session-btn:hover {
  background: rgba(255, 255, 255, 0.05);
}
.bd-page__session-slot {
  grid-row: span 2;
  min-width: 56px;
  color: var(--color-spark);
  font-family: var(--font-display);
  font-size: 18px;
  font-weight: 700;
  line-height: 1.1;
}
.bd-page__session-status {
  display: inline-block;
  width: max-content;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
}
.bd-page__session-status[data-status='playing'] {
  background: rgba(var(--color-primary-rgb), 0.18);
  color: var(--color-primary-light);
}
.bd-page__session-status[data-status='ended'] {
  background: rgba(148, 163, 184, 0.18);
  color: #94a3b8;
}
.bd-page__session-progress {
  color: rgba(255, 255, 255, 0.7);
}
.bd-page__session-date {
  grid-column: 2;
  color: rgba(255, 255, 255, 0.4);
  font-size: 12px;
}

@media (max-width: 768px) {
  .bd-page {
    gap: 8px;
    padding: 10px;
    padding-top: calc(10px + var(--safe-area-top, 0px));
    padding-bottom: calc(10px + var(--safe-area-bottom, 0px));
    padding-left: calc(10px + var(--safe-area-left, 0px));
    padding-right: calc(10px + var(--safe-area-right, 0px));
  }
  .bd-page.is-embedded:not(.is-playing) {
    padding: 0;
  }
  .bd-page__brand h1 {
    font-size: 17px;
  }
  .bd-page__back-label {
    display: none;
  }
  .bd-page__menu-btn {
    display: inline-flex;
  }
  .bd-page__hint {
    font-size: 11px;
    line-height: 1.5;
  }
  .bd-page__layout {
    grid-template-columns: 1fr;
  }
  .bd-page__sidebar {
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
  .bd-page__sidebar.is-open {
    transform: translateX(0);
  }
  .bd-page__scrim {
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
  .bd-page__title-screen {
    min-height: 220px;
    padding: var(--space-4);
  }
  .bd-page__title-copy h2 {
    font-size: 30px;
  }
  .bd-page__detail > p,
  .bd-page__detail-prompt,
  .bd-page__detail-chars,
  .bd-page__detail-actions,
  .bd-page__sessions,
  .bd-page__progress {
    margin-inline: var(--space-4);
  }
  .bd-page__actions {
    flex-direction: column;
  }
}

@media (prefers-reduced-motion: reduce) {
  .bd-page__new-game,
  .bd-page__progress-label,
  .bd-page__progress-fill::after {
    animation: none;
  }

  .bd-page__session-item,
  .bd-page__session-item:hover {
    transform: none;
    transition: none;
  }
}
</style>
