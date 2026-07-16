<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  createStorySeed,
  deleteStorySeed,
  listStoryEvents,
  listStorySeeds,
  rollStoryEvent,
  updateStorySeed,
} from '../utils/api/story'
import type { StoryEvent, StorySeed } from '../types/story'
import StoryArcPanel from './StoryArcPanel.vue'
import { UiButton } from './ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const props = defineProps<{
  characterId: string
  worldFrame: string
  /**
   * Bound arc-template id (Phase 2 of SCENE_BEAT_PLAN). Forwarded to
   * the inner StoryArcPanel; ``null`` = LLM planning fallback.
   */
  arcTemplateId?: string | null
}>()
const emit = defineEmits<{
  (e: 'update:worldFrame', v: string): void
  /**
   * Bubble template binding changes up to the sidebar so it can PATCH
   * the character. ``null`` clears the binding back to LLM planning.
   */
  (e: 'update:arc-template', templateId: string | null): void
  (e: 'active-arc-change', hasActiveArc: boolean): void
}>()

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const frameChoices = ['modern', 'fantasy', 'school', 'custom']

function frameLabel(frame: string): string {
  return t(`story.panel.worldFrameOptions.${frame}`)
}

const events = ref<StoryEvent[]>([])
const seeds = ref<StorySeed[]>([])
const eventsLoading = ref(false)
const seedsLoading = ref(false)
const rolling = ref(false)
const error = ref<string | null>(null)
const storyArcPanel = ref<InstanceType<typeof StoryArcPanel> | null>(null)

const newSeedText = ref('')
const newSeedFrames = ref<string>('any')

function onFrameChange(event: Event) {
  const target = event.target as HTMLSelectElement
  emit('update:worldFrame', target.value)
}

function handleActiveArcChange(next: boolean) {
  emit('active-arc-change', next)
}

/**
 * Story-arc actions are triggered from the hoisted ArcDiscoveryCard that the
 * sidebar renders above the (collapsed) story section. Expose them so the
 * parent can open the new-arc modal / template picker without the player
 * having to expand the section first.
 */
function openNewArc() {
  storyArcPanel.value?.openNewArc()
}

function openTemplatePicker() {
  storyArcPanel.value?.openTemplatePicker()
}

defineExpose({ openNewArc, openTemplatePicker })

async function reloadEvents() {
  if (!props.characterId) return
  eventsLoading.value = true
  error.value = null
  try {
    events.value = await listStoryEvents(props.characterId, 10)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    eventsLoading.value = false
  }
}

async function reloadSeeds() {
  if (!props.characterId) return
  seedsLoading.value = true
  error.value = null
  try {
    seeds.value = await listStorySeeds(props.characterId, { includeGlobal: true, enabledOnly: false })
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    seedsLoading.value = false
  }
}

async function handleRoll() {
  if (!props.characterId) return
  rolling.value = true
  error.value = null
  try {
    events.value = await rollStoryEvent(props.characterId)
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    rolling.value = false
  }
}

async function handleCreateSeed() {
  const text = newSeedText.value.trim()
  if (!text) return
  try {
    const frames = newSeedFrames.value.trim().split(',').map((s) => s.trim()).filter(Boolean)
    await createStorySeed(props.characterId, {
      seed_text: text,
      world_frames: frames.length ? frames : ['any'],
    })
    newSeedText.value = ''
    await reloadSeeds()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function toggleSeed(seed: StorySeed) {
  try {
    if (seed.external_id || seed.pack_id) {
      // packed seeds: backend refuses edits; we skip.
      return
    }
    await updateStorySeed(seed.id, { enabled: !seed.enabled })
    await reloadSeeds()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

async function removeSeed(seed: StorySeed) {
  if (seed.external_id || seed.pack_id) return
  if (!await confirmDialog({
    content: t('story.panel.confirmDeleteSeed', { seed: seed.seed_text }),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  try {
    await deleteStorySeed(seed.id)
    await reloadSeeds()
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  }
}

function sourceLabel(seed: StorySeed): string {
  if (seed.pack_id) return t('story.panel.seedSourcePack', { pack: seed.pack_id })
  if (seed.character_id) return t('story.panel.seedSourceCharacter')
  return t('story.panel.seedSourceGlobal')
}

onMounted(async () => {
  await Promise.all([reloadEvents(), reloadSeeds()])
})

watch(() => props.characterId, async () => {
  events.value = []
  seeds.value = []
  if (props.characterId) {
    await Promise.all([reloadEvents(), reloadSeeds()])
  }
})
</script>

<template>
  <div class="story-panel">
    <!-- 劇情主軸（跨週 arc）— 放在最上方，是這個角色「世界的骨架」 -->
    <StoryArcPanel
      ref="storyArcPanel"
      :character-id="characterId"
      :arc-template-id="arcTemplateId ?? null"
      :world-frame="worldFrame"
      @update:arc-template="(id) => emit('update:arc-template', id)"
      @active-arc-change="handleActiveArcChange"
    />

    <div class="section-divider" aria-hidden="true"></div>

    <p class="field-hint">
      {{ t('story.panel.hint') }}
    </p>

    <div class="frame-block">
      <label class="field-label">{{ t('story.panel.worldFrameLabel') }}</label>
      <select :value="worldFrame" class="field-select" @change="onFrameChange">
        <option v-for="f in frameChoices" :key="f" :value="f">{{ frameLabel(f) }}</option>
      </select>
    </div>

    <div class="events-block">
      <div class="block-header">
        <h4 class="block-title">{{ t('story.panel.eventsTitle') }}</h4>
        <div class="block-actions">
          <UiButton size="sm" :loading="eventsLoading" @click="reloadEvents">
            {{ eventsLoading ? t('common.state.loading') : t('story.panel.reload') }}
          </UiButton>
          <UiButton variant="primary" size="sm" :loading="rolling" @click="handleRoll">
            {{ rolling ? t('story.panel.rolling') : t('story.panel.rollNow') }}
          </UiButton>
        </div>
      </div>
      <div v-if="events.length === 0 && !eventsLoading" class="empty">
        {{ t('story.panel.eventsEmpty') }}
      </div>
      <ul v-else class="event-list">
        <li v-for="evt in events" :key="evt.id" class="event-item">
          <div class="event-meta">
            <span class="event-date">{{ evt.date }}</span>
            <span v-if="evt.emotional_tone" class="tone-badge">{{ evt.emotional_tone }}</span>
            <span v-if="evt.memorialized" class="mem-badge">{{ t('story.panel.memorialized') }}</span>
          </div>
          <div class="event-narrative">{{ evt.narrative }}</div>
        </li>
      </ul>
    </div>

    <div class="seeds-block">
      <div class="block-header">
        <h4 class="block-title">{{ t('story.panel.seedsTitle') }}</h4>
        <UiButton size="sm" :loading="seedsLoading" @click="reloadSeeds">
          {{ seedsLoading ? t('common.state.loading') : t('story.panel.reload') }}
        </UiButton>
      </div>

      <div class="seed-create">
        <input
          v-model="newSeedText"
          class="field-input"
          :placeholder="t('story.panel.newSeedPlaceholder')"
          @keydown.enter.prevent="handleCreateSeed"
        />
        <input
          v-model="newSeedFrames"
          class="field-input frame-input"
          :placeholder="t('story.panel.seedFramesPlaceholder')"
        />
        <UiButton variant="primary" size="sm" @click="handleCreateSeed">{{ t('story.panel.addSeed') }}</UiButton>
      </div>

      <div v-if="seeds.length === 0 && !seedsLoading" class="empty">
        {{ t('story.panel.seedsEmptyPrefix') }} <code>import_story_seeds</code>
        {{ t('story.panel.seedsEmptySuffix') }}
      </div>
      <ul v-else class="seed-list">
        <li v-for="seed in seeds" :key="seed.id" :class="{ 'seed-item': true, disabled: !seed.enabled }">
          <div class="seed-row">
            <input
              type="checkbox"
              :checked="seed.enabled"
              :disabled="!!(seed.external_id || seed.pack_id)"
              :title="(seed.external_id || seed.pack_id) ? t('story.panel.packSeedReadonly') : ''"
              @change="toggleSeed(seed)"
            />
            <span class="seed-text">{{ seed.seed_text }}</span>
          </div>
          <div class="seed-meta">
            <span>{{ sourceLabel(seed) }}</span>
            <span>frames: {{ seed.world_frames.join(', ') }}</span>
            <span>cooldown: {{ seed.cooldown_days }}d</span>
            <span v-if="!seed.external_id && !seed.pack_id">
              <button class="btn-link" @click="removeSeed(seed)">{{ t('common.actions.delete') }}</button>
            </span>
          </div>
        </li>
      </ul>
    </div>

    <div v-if="error" class="error">{{ error }}</div>
  </div>
</template>

<style scoped>
.story-panel { display: flex; flex-direction: column; gap: 14px; }
.section-divider {
  height: 1px;
  background: var(--color-border);
  margin: 4px 0;
}
/* .field-hint 在 global style.css；此面板 hint 字級較大 */
.field-hint { font-size: 13px; }
.frame-block { display: flex; flex-direction: column; gap: 4px; }
/* 共用欄位樣式在 global style.css */
.block-header {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.block-title { margin: 0; font-size: 14px; }
.block-actions { display: flex; gap: 6px; }
.btn-link {
  background: none; border: none; color: var(--color-danger, #e07070);
  cursor: pointer; font-size: 12px; padding: 0;
}
.empty { color: var(--color-muted, #888); font-size: 13px; }
.event-list, .seed-list {
  list-style: none; margin: 0; padding: 0;
  display: flex; flex-direction: column; gap: 8px;
  max-height: 280px; overflow-y: auto;
}
.event-item, .seed-item {
  background: var(--color-surface-2, rgba(255, 255, 255, 0.04));
  border-radius: 6px;
  padding: 8px 10px;
  font-size: 13px;
}
.event-meta {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
  font-size: 12px;
  color: var(--color-muted, #888);
}
.event-date {
  flex: 0 1 auto;
  max-width: 100%;
  font-variant-numeric: tabular-nums;
  overflow-wrap: anywhere;
}
.tone-badge {
  background: rgba(100, 180, 255, 0.2); color: #9ecbff;
  max-width: 100%;
  padding: 1px 6px; border-radius: 4px; font-size: 11px;
  overflow-wrap: anywhere;
}
.mem-badge {
  background: rgba(109, 213, 140, 0.15); color: #6dd58c;
  max-width: 100%;
  padding: 1px 6px; border-radius: 4px; font-size: 11px;
  overflow-wrap: anywhere;
}
.event-narrative { margin-top: 6px; line-height: 1.5; overflow-wrap: anywhere; }
.seed-create { display: flex; gap: 6px; flex-wrap: wrap; }
.seed-create .field-input { flex: 1; min-width: 160px; }
.seed-create .frame-input { flex-basis: 150px; flex-grow: 0; }
.seed-row { display: flex; align-items: center; gap: 6px; }
.seed-text { line-height: 1.4; }
.seed-meta {
  display: flex; gap: 10px; margin-top: 4px; font-size: 11px;
  color: var(--color-muted, #888);
}
.seed-item.disabled .seed-text { opacity: 0.5; text-decoration: line-through; }
.error { color: var(--color-danger, #e07070); font-size: 13px; }
</style>
