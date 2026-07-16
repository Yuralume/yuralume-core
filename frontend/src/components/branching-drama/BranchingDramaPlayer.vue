<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type {
  BranchingDrama,
  DramaNode,
  DramaSession,
  DramaSessionTurn,
  Exchange,
} from '@/types/branchingDrama'
import {
  advanceSession,
  endDramaSession,
  getDramaNode,
  getSession,
  interactSession,
  startSession,
} from '@/utils/api/branchingDrama'
import { resolveSceneImageUrl } from '@/utils/sceneImage'

const { t } = useI18n()

const props = defineProps<{
  drama: BranchingDrama
  characters: Character[]
  resumeSessionId?: string | null
}>()

const emit = defineEmits<{
  (e: 'exit'): void
  (e: 'error', msg: string): void
}>()

const session = ref<DramaSession | null>(null)
const currentNode = ref<DramaNode | null>(null)
const narrationText = ref('')
const playerInput = ref('')
const loading = ref(false)
const reachedEnd = ref(false)
const showEndOverlay = ref(false)
const displayedTurns = ref<DramaSessionTurn[]>([])
const advanceHint = ref<string | null>(null)
const pendingExchanges = ref<Exchange[]>([])
const atFinalBeat = ref(false)

const charMap = computed(() => {
  const m = new Map<string, Character>()
  for (const c of props.characters) m.set(c.id, c)
  return m
})

const currentImageUrl = computed(() => {
  if (!currentNode.value?.image_path) return null
  return resolveSceneImageUrl(currentNode.value.image_path)
})

const appearingCharNames = computed(() => {
  if (!currentNode.value) return ''
  return currentNode.value.appearing_character_ids
    .map((id) => charMap.value.get(id)?.name ?? '???')
    .join(t('common.listSeparator'))
})

const advanceButtonText = computed(() => {
  if (atFinalBeat.value) {
    return advanceHint.value
      ? t('branchingDrama.player.endWithHint', { hint: advanceHint.value })
      : t('branchingDrama.player.endStory')
  }
  if (advanceHint.value) return t('branchingDrama.player.advanceWithHint', { hint: advanceHint.value })
  return t('branchingDrama.player.advance')
})

const scrollRef = ref<HTMLElement | null>(null)

function scrollToBottom() {
  nextTick(() => {
    if (scrollRef.value) {
      scrollRef.value.scrollTop = scrollRef.value.scrollHeight
    }
  })
}

async function begin() {
  loading.value = true
  reachedEnd.value = false
  showEndOverlay.value = false
  displayedTurns.value = []
  advanceHint.value = null
  pendingExchanges.value = []
  atFinalBeat.value = false
  try {
    let sess: DramaSession
    if (props.resumeSessionId) {
      sess = await getSession(props.drama.id, props.resumeSessionId)
    } else {
      sess = await startSession(props.drama.id)
    }
    session.value = sess
    reachedEnd.value = sess.status === 'ended'
    showEndOverlay.value = false
    if (sess.turns.length > 0) {
      displayedTurns.value = [...sess.turns]
      const lastTurn = sess.turns[sess.turns.length - 1]
      narrationText.value = lastTurn.narration
      pendingExchanges.value = [...(lastTurn.exchanges ?? [])]
      const node = await getDramaNode(props.drama.id, lastTurn.node_id)
      currentNode.value = node
      atFinalBeat.value = node.depth >= props.drama.total_segments - 1
    }
    scrollToBottom()
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('branchingDrama.player.errors.startFailed'))
  } finally {
    loading.value = false
  }
}

async function handleInteract() {
  if (!session.value || !playerInput.value.trim() || loading.value) return
  const input = playerInput.value.trim()
  playerInput.value = ''
  loading.value = true
  try {
    const result = await interactSession(
      props.drama.id,
      session.value.id,
      input,
    )
    session.value = result.session
    advanceHint.value = result.advance_hint
    pendingExchanges.value.push({ player_input: input, response: result.response })
    displayedTurns.value = [...result.session.turns]
    scrollToBottom()
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('branchingDrama.player.errors.interactFailed'))
  } finally {
    loading.value = false
  }
}

async function handleAdvance() {
  if (!session.value || loading.value) return
  loading.value = true
  try {
    if (atFinalBeat.value) {
      const sess = await endDramaSession(props.drama.id, session.value.id)
      session.value = sess
      reachedEnd.value = true
      showEndOverlay.value = false
      displayedTurns.value = [...sess.turns]
      scrollToBottom()
      return
    }
    const result = await advanceSession(
      props.drama.id,
      session.value.id,
    )
    session.value = result.session
    currentNode.value = result.current_node
    atFinalBeat.value = result.is_ending
    showEndOverlay.value = false
    displayedTurns.value = [...result.session.turns]
    narrationText.value =
      result.session.turns[result.session.turns.length - 1]?.narration ?? ''
    advanceHint.value = null
    pendingExchanges.value = []
    scrollToBottom()
  } catch (err: unknown) {
    emit('error', err instanceof Error ? err.message : t('branchingDrama.player.errors.advanceFailed'))
  } finally {
    loading.value = false
  }
}

function handleKeydown(ev: KeyboardEvent) {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault()
    handleInteract()
  }
}

watch(
  () => props.drama.id,
  () => {
    session.value = null
    currentNode.value = null
    narrationText.value = ''
    displayedTurns.value = []
    reachedEnd.value = false
    showEndOverlay.value = false
    advanceHint.value = null
    pendingExchanges.value = []
    atFinalBeat.value = false
  },
)

begin()
</script>

<template>
  <div class="vn-player">
    <!-- background scene image -->
    <div
      class="vn-player__bg"
      :style="{
        backgroundImage: currentImageUrl
          ? `url(${currentImageUrl})`
          : undefined,
      }"
    >
      <div v-if="!currentImageUrl" class="vn-player__bg-fallback" />
    </div>

    <!-- top bar -->
    <header class="vn-player__header">
      <button class="vn-player__exit" @click="$emit('exit')">
        &larr; {{ t('branchingDrama.player.backToList') }}
      </button>
      <span class="vn-player__title">{{ drama.title }}</span>
      <span v-if="currentNode" class="vn-player__depth">
        {{ currentNode.depth + 1 }} / {{ drama.total_segments }}
      </span>
    </header>

    <!-- dialogue scroll area -->
    <div ref="scrollRef" class="vn-player__dialogue-scroll">
      <template v-for="(turn, i) in displayedTurns" :key="i">
        <!-- narration (opening scene for this beat) -->
        <div class="vn-player__bubble vn-player__bubble--narration">
          <div class="vn-player__bubble-label">
            {{ appearingCharNames || t('branchingDrama.player.scene') }}
            <span v-if="turn.chosen_tone" class="vn-player__tone-tag" :data-tone="turn.chosen_tone">
              {{ turn.chosen_tone }}
            </span>
          </div>
          <div class="vn-player__bubble-text">{{ turn.narration }}</div>
        </div>
        <!-- exchanges within this beat -->
        <template v-for="(ex, j) in turn.exchanges" :key="`${i}-ex-${j}`">
          <div class="vn-player__bubble vn-player__bubble--player">
            <div class="vn-player__bubble-label">{{ t('branchingDrama.player.you') }}</div>
            <div class="vn-player__bubble-text">{{ ex.player_input }}</div>
          </div>
          <div class="vn-player__bubble vn-player__bubble--narration vn-player__bubble--exchange">
            <div class="vn-player__bubble-label">
              {{ appearingCharNames || t('branchingDrama.player.scene') }}
            </div>
            <div class="vn-player__bubble-text">{{ ex.response }}</div>
          </div>
        </template>
      </template>

      <div v-if="loading" class="vn-player__loading">
        {{ t('branchingDrama.player.thinking') }}
      </div>

      <!-- end prompt (shown after final narration, before overlay) -->
      <div v-if="reachedEnd && !loading && !showEndOverlay" class="vn-player__end-prompt">
        <button class="vn-player__btn vn-player__btn--end" @click="showEndOverlay = true">
          ~ Fin ~
        </button>
      </div>
    </div>

    <!-- ending overlay -->
    <div v-if="showEndOverlay" class="vn-player__ending">
      <div class="vn-player__ending-card">
        <h2>~ End ~</h2>
        <p>{{ drama.title }}</p>
        <button class="vn-player__btn" @click="$emit('exit')">
          {{ t('branchingDrama.player.backToList') }}
        </button>
        <button class="vn-player__btn vn-player__btn--secondary" @click="begin()">
          {{ t('branchingDrama.player.replay') }}
        </button>
      </div>
    </div>

    <!-- input area (hidden on ending) -->
    <footer v-if="session && !reachedEnd" class="vn-player__input-bar">
      <div class="vn-player__input-row">
        <textarea
          v-model="playerInput"
          class="field-textarea"
          :disabled="loading"
          rows="2"
          :placeholder="t('branchingDrama.player.inputPlaceholder')"
          @keydown="handleKeydown"
        />
        <button
          class="vn-player__send"
          :disabled="loading || !playerInput.trim()"
          @click="handleInteract"
        >
          {{ loading ? '…' : t('common.actions.submit') }}
        </button>
      </div>
      <button
        class="vn-player__advance"
        :class="{
          'vn-player__advance--hinted': !!advanceHint,
          'vn-player__advance--final': atFinalBeat,
        }"
        :disabled="loading"
        @click="handleAdvance"
      >
        {{ advanceButtonText }}
      </button>
    </footer>

    <!-- initial loading -->
    <div v-if="!session && loading" class="vn-player__init-loading">
      {{ t('branchingDrama.player.starting') }}
    </div>
  </div>
</template>

<style scoped>
.vn-player {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
  overflow: hidden;
  border-radius: 8px;
  background: var(--color-bg);
}

.vn-player__bg {
  position: absolute;
  inset: 0;
  background-size: cover;
  background-position: center;
  z-index: 0;
}
.vn-player__bg::after {
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(
    to bottom,
    rgba(0, 0, 0, 0.3) 0%,
    rgba(0, 0, 0, 0.6) 50%,
    rgba(0, 0, 0, 0.85) 100%
  );
}
.vn-player__bg-fallback {
  width: 100%;
  height: 100%;
  background: linear-gradient(135deg, #1a1a2e 0%, #16213e 40%, #0f3460 100%);
}

.vn-player__header {
  position: relative;
  z-index: 2;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: rgba(0, 0, 0, 0.5);
  backdrop-filter: blur(6px);
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}
.vn-player__exit {
  background: none;
  border: none;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  font-size: 13px;
  padding: 4px 8px;
  border-radius: 4px;
}
.vn-player__exit:hover {
  color: #fff;
  background: rgba(255, 255, 255, 0.08);
}
.vn-player__title {
  flex: 1;
  font-weight: 600;
  font-size: 15px;
  color: rgba(255, 255, 255, 0.9);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.vn-player__depth {
  font-size: 12px;
  color: rgba(255, 255, 255, 0.5);
  white-space: nowrap;
}

.vn-player__dialogue-scroll {
  position: relative;
  z-index: 2;
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.vn-player__bubble {
  max-width: 85%;
  padding: 10px 14px;
  border-radius: 12px;
  animation: fadeSlide 0.3s ease;
}
@keyframes fadeSlide {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}
.vn-player__bubble--narration {
  align-self: flex-start;
  background: rgba(255, 255, 255, 0.08);
  backdrop-filter: blur(4px);
  border: 1px solid rgba(255, 255, 255, 0.1);
}
.vn-player__bubble--player {
  align-self: flex-end;
  background: rgba(var(--color-primary-rgb), 0.18);
  border: 1px solid rgba(var(--color-primary-rgb), 0.3);
}
.vn-player__bubble-label {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.55);
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.vn-player__bubble-text {
  font-size: 14px;
  line-height: 1.7;
  color: rgba(255, 255, 255, 0.92);
  white-space: pre-wrap;
}

.vn-player__tone-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.vn-player__tone-tag[data-tone='dark'] {
  background: rgba(190, 24, 93, 0.25);
  color: #f472b6;
}
.vn-player__tone-tag[data-tone='sunny'] {
  background: rgba(234, 179, 8, 0.25);
  color: #fde047;
}
.vn-player__tone-tag[data-tone='neutral'] {
  background: rgba(148, 163, 184, 0.25);
  color: #cbd5e1;
}

.vn-player__loading {
  text-align: center;
  color: rgba(255, 255, 255, 0.5);
  font-size: 13px;
  padding: 12px;
  animation: pulse 1.5s infinite;
}
@keyframes pulse {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}

.vn-player__end-prompt {
  display: flex;
  justify-content: center;
  padding: 16px 0 8px;
}
.vn-player__btn--end {
  background: rgba(var(--color-primary-rgb), 0.2);
  border: 1px solid rgba(var(--color-primary-rgb), 0.5);
  color: var(--color-primary-light);
  padding: 12px 32px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 16px;
  font-weight: 300;
  letter-spacing: 0.15em;
  transition: background 0.2s;
}
.vn-player__btn--end:hover {
  background: rgba(var(--color-primary-rgb), 0.35);
}

.vn-player__ending {
  position: absolute;
  inset: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.8);
  backdrop-filter: blur(8px);
  animation: fadeIn 0.6s ease;
}
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}
.vn-player__ending-card {
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 32px 48px;
}
.vn-player__ending-card h2 {
  font-size: 32px;
  font-weight: 300;
  color: rgba(255, 255, 255, 0.9);
  margin: 0;
  letter-spacing: 0.15em;
}
.vn-player__ending-card p {
  font-size: 16px;
  color: rgba(255, 255, 255, 0.6);
  margin: 0;
}

.vn-player__btn {
  background: rgba(var(--color-primary-rgb), 0.25);
  border: 1px solid rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
  padding: 10px 20px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 14px;
}
.vn-player__btn:hover {
  background: rgba(var(--color-primary-rgb), 0.35);
}
.vn-player__btn--secondary {
  background: rgba(255, 255, 255, 0.06);
  border-color: rgba(255, 255, 255, 0.18);
  color: rgba(255, 255, 255, 0.7);
}
.vn-player__btn--secondary:hover {
  background: rgba(255, 255, 255, 0.1);
}

.vn-player__input-bar {
  position: relative;
  z-index: 2;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(6px);
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}
.vn-player__input-row {
  display: flex;
  align-items: flex-end;
  gap: 8px;
}
.vn-player__input-row .field-textarea {
  flex: 1;
  resize: none;
  line-height: 1.5;
}
.vn-player__send {
  background: rgba(var(--color-primary-rgb), 0.3);
  border: 1px solid rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 14px;
  white-space: nowrap;
  align-self: stretch;
}
.vn-player__send:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.vn-player__send:hover:not(:disabled) {
  background: rgba(var(--color-primary-rgb), 0.4);
}
.vn-player__advance {
  background: rgba(var(--color-primary-rgb), 0.12);
  border: 1px solid rgba(var(--color-primary-rgb), 0.3);
  color: rgba(var(--color-primary-rgb), 0.7);
  padding: 8px 16px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.25s ease;
}
.vn-player__advance:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.vn-player__advance:hover:not(:disabled) {
  background: rgba(var(--color-primary-rgb), 0.2);
  border-color: rgba(var(--color-primary-rgb), 0.5);
}
.vn-player__advance--hinted {
  background: rgba(var(--color-primary-rgb), 0.2);
  border-color: rgba(var(--color-primary-rgb), 0.55);
  color: var(--color-primary-light);
  animation: hintPulse 2s ease-in-out infinite;
}
@keyframes hintPulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(var(--color-primary-rgb), 0); }
  50% { box-shadow: 0 0 12px 2px rgba(var(--color-primary-rgb), 0.2); }
}
.vn-player__advance--final {
  background: rgba(234, 179, 8, 0.15);
  border-color: rgba(234, 179, 8, 0.4);
  color: #fde047;
}
.vn-player__advance--final:hover:not(:disabled) {
  background: rgba(234, 179, 8, 0.25);
  border-color: rgba(234, 179, 8, 0.6);
}
.vn-player__bubble--exchange {
  border-left: 2px solid rgba(var(--color-primary-rgb), 0.3);
}

.vn-player__init-loading {
  position: absolute;
  inset: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
  color: rgba(255, 255, 255, 0.6);
  font-size: 16px;
  animation: pulse 1.5s infinite;
}

@media (max-width: 768px) {
  .vn-player__bubble {
    max-width: 95%;
  }
  .vn-player__dialogue-scroll {
    padding: 10px;
    gap: 8px;
  }
  .vn-player__input-bar {
    padding: 8px 10px;
  }
  .vn-player__ending-card {
    padding: 24px 20px;
  }
}
</style>
