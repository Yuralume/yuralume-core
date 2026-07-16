<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { Character } from '@/types/character'
import type { UpdateCharacterRequest } from '@/types/character'
import { updateCharacter } from '@/utils/api/characters'
import InterestSubscriptionPanel from '@/components/InterestSubscriptionPanel.vue'
import WorldAwarenessPanel from '@/components/WorldAwarenessPanel.vue'
import { UiCard, UiButton } from '@/components/ui'
import { useI18n } from 'vue-i18n'

/**
 * Editor for one character's external-event subscription state. Composes
 * the canonical category picker (InterestSubscriptionPanel) with the legacy
 * free-form topic filter + event-pool preview (WorldAwarenessPanel) onto a
 * single PATCH (one Save button writes all four fields).
 *
 * Designed to be remounted via ``:key="character.id"`` from the parent — that
 * keeps setup() honest and avoids stale-form bugs when switching characters.
 */
type EditorSurface = 'admin' | 'player'

const props = withDefaults(defineProps<{
  character: Character
  patch: (updated: Character) => void
  surface?: EditorSurface
  includeWorldFrame?: boolean
  showEventPoolPreview?: boolean
}>(), {
  surface: 'admin',
  includeWorldFrame: true,
  showEventPoolPreview: true,
})

const { t } = useI18n()

interface WorldForm {
  world_frame: string
  world_awareness_enabled: boolean
  subscribed_categories: string[]
  excluded_topics: string[]
  world_topics: string[]
}

function snapshot(char: Character): WorldForm {
  return {
    world_frame: char.world_frame || 'modern',
    world_awareness_enabled: char.world_awareness_enabled ?? false,
    subscribed_categories: [...(char.subscribed_categories ?? [])],
    excluded_topics: [...(char.excluded_topics ?? [])],
    world_topics: [...(char.world_topics ?? [])],
  }
}

const form = ref<WorldForm>(snapshot(props.character))
const saving = ref(false)
const errorMsg = ref<string | null>(null)
const successMsg = ref<string | null>(null)
const isPlayerSurface = computed(() => props.surface === 'player')
const copyRoot = computed(() => (
  isPlayerSurface.value ? 'playerAuthoring.worldEditor' : 'admin.worldEditor'
))

function copy(path: string, params?: Record<string, unknown>): string {
  return t(`${copyRoot.value}.${path}`, params ?? {})
}

async function handleSave() {
  saving.value = true
  errorMsg.value = null
  successMsg.value = null
  try {
    const payload: UpdateCharacterRequest = {
      world_awareness_enabled: form.value.world_awareness_enabled,
      subscribed_categories: [...form.value.subscribed_categories],
      excluded_topics: [...form.value.excluded_topics],
      world_topics: [...form.value.world_topics],
    }
    if (props.includeWorldFrame) {
      payload.world_frame = form.value.world_frame
    }
    const updated = await updateCharacter(props.character.id, payload)
    props.patch(updated)
    successMsg.value = copy('saved')
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : copy('saveFailed')
  } finally {
    saving.value = false
  }
}

watch(successMsg, (next) => {
  if (!next) return
  setTimeout(() => {
    if (successMsg.value === next) successMsg.value = null
  }, 2500)
})
</script>

<template>
  <div :class="['world-editor', { 'world-editor--player': isPlayerSurface }]">
    <UiCard v-if="includeWorldFrame" size="lg">
      <template #header>
        <h2 class="world-editor__card-title">{{ copy('worldFrameTitle') }}</h2>
      </template>

      <div class="world-editor__field">
        <label class="field-label">{{ copy('worldFrameLabel') }}</label>
        <select v-model="form.world_frame" class="field-select">
          <option value="modern">{{ t('characterCreate.fields.worldFrame.options.modern') }}</option>
          <option value="fantasy">{{ t('characterCreate.fields.worldFrame.options.fantasy') }}</option>
          <option value="school">{{ t('characterCreate.fields.worldFrame.options.school') }}</option>
          <option value="custom">{{ t('characterCreate.fields.worldFrame.options.custom') }}</option>
        </select>
        <div class="field-hint">{{ copy('worldFrameHint') }}</div>
      </div>
    </UiCard>

    <UiCard v-if="!isPlayerSurface" size="lg">
      <template #header>
        <h2 class="world-editor__card-title">
          {{ copy('subscriptionTitle', { name: character.name }) }}
        </h2>
      </template>

      <InterestSubscriptionPanel
        v-model:model-enabled="form.world_awareness_enabled"
        v-model:model-categories="form.subscribed_categories"
        v-model:model-excluded="form.excluded_topics"
      />
    </UiCard>

    <section v-else class="world-editor__player-panel">
      <h3 class="world-editor__player-title">
        {{ copy('subscriptionTitle', { name: character.name }) }}
      </h3>
      <InterestSubscriptionPanel
        v-model:model-enabled="form.world_awareness_enabled"
        v-model:model-categories="form.subscribed_categories"
        v-model:model-excluded="form.excluded_topics"
        copy-namespace="playerAuthoring.interestSubscriptionPanel"
        category-namespace="interestSubscriptionPanel.categories"
      />
    </section>

    <UiCard v-if="!isPlayerSurface" size="lg">
      <template #header>
        <h2 class="world-editor__card-title">{{ copy('eventPoolTitle') }}</h2>
      </template>

      <p class="world-editor__note">
        {{ copy('eventPoolNotePrefix') }} <code>world_topics</code>
        {{ copy('eventPoolNoteSuffix') }}
      </p>

      <WorldAwarenessPanel
        v-model:model-enabled="form.world_awareness_enabled"
        v-model:model-topics="form.world_topics"
        :show-preview="showEventPoolPreview"
      />
    </UiCard>

    <section v-else class="world-editor__player-panel">
      <h3 class="world-editor__player-title">
        {{ copy('topicDetailTitle') }}
      </h3>
      <WorldAwarenessPanel
        v-model:model-enabled="form.world_awareness_enabled"
        v-model:model-topics="form.world_topics"
        copy-namespace="playerAuthoring.worldTopicsPanel"
        :show-enabled-toggle="false"
        :show-preview="false"
      />
    </section>

    <div class="world-editor__actions">
      <div class="world-editor__status">
        <span v-if="errorMsg" class="world-editor__error">{{ errorMsg }}</span>
        <span v-else-if="successMsg" class="world-editor__success">{{ successMsg }}</span>
      </div>
      <UiButton
        variant="primary"
        :loading="saving"
        :disabled="saving"
        @click="handleSave"
      >{{ saving ? t('common.state.saving') : copy('saveAction') }}</UiButton>
    </div>
  </div>
</template>

<style scoped>
.world-editor {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.world-editor--player {
  gap: var(--space-3);
}
.world-editor__card-title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 600;
}
.world-editor__player-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-bottom: var(--space-3);
  border-bottom: 1px dashed var(--color-border);
}
.world-editor__player-title {
  margin: 0;
  font-size: var(--font-sm);
  font-weight: 600;
  color: var(--color-primary-light);
}
.world-editor__field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
.world-editor__note {
  margin: 0 0 var(--space-2);
  padding: var(--space-2) var(--space-3);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  line-height: 1.6;
}
.world-editor__actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--space-3);
}
.world-editor__status {
  flex: 1;
  font-size: var(--font-sm);
}
.world-editor__error {
  color: #f4a3a3;
}
.world-editor__success {
  color: #6dd58c;
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
}
</style>
