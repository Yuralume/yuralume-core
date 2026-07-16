<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  EditOutlined,
  EyeOutlined,
  ReloadOutlined,
} from '@ant-design/icons-vue'

import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { UiButton } from '@/components/ui'
import {
  getOperatorPersonaProjection,
  transitionPersonaFieldState,
  type PersonaProjection,
  type PersonaProjectionFact,
} from '@/utils/api/operatorPersona'

const props = defineProps<{
  characterId: string | null | undefined
}>()

const emit = defineEmits<{
  corrected: []
}>()

const { t, te } = useI18n()
const confirmDialog = useConfirmDialog()

const projection = ref<PersonaProjection | null>(null)
const loading = ref(false)
const correctingFieldId = ref<string | null>(null)
const errorMsg = ref<string | null>(null)

const visibleFacts = computed(() => projection.value?.facts ?? [])
const hasFacts = computed(() => visibleFacts.value.length > 0)
const narrative = computed(() => projection.value?.narrative.trim() ?? '')

/**
 * Prefer the trilingual bundle keyed on the stable `field_key` (plan
 * D6); fall back to the backend-provided zh-TW `label` when the key is
 * absent (older backend) or unmapped. `te()` guards against firing a
 * missing-key warning for unknown keys.
 */
function factLabel(fact: PersonaProjectionFact): string {
  const key = fact.field_key
  if (key) {
    const bundleKey = `memoir.personaProjection.factLabels.${key}`
    if (te(bundleKey)) return t(bundleKey)
  }
  return fact.label
}

async function loadProjection() {
  if (!props.characterId) {
    projection.value = null
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    projection.value = await getOperatorPersonaProjection(props.characterId)
  }
  catch (err) {
    projection.value = null
    errorMsg.value = err instanceof Error
      ? err.message
      : t('memoir.personaProjection.errors.loadFailed')
  }
  finally {
    loading.value = false
  }
}

async function correctFact(fact: PersonaProjectionFact) {
  if (!await confirmDialog({
    content: t('memoir.personaProjection.confirmCorrect', {
      label: factLabel(fact),
      value: fact.value,
    }),
    okText: t('memoir.personaProjection.correctAction'),
  })) {
    return
  }
  correctingFieldId.value = fact.field_id
  errorMsg.value = null
  try {
    await transitionPersonaFieldState(fact.field_id, 'rejected')
    await loadProjection()
    emit('corrected')
  }
  catch (err) {
    errorMsg.value = err instanceof Error
      ? err.message
      : t('memoir.personaProjection.errors.correctFailed')
  }
  finally {
    correctingFieldId.value = null
  }
}

watch(() => props.characterId, loadProjection, { immediate: true })
</script>

<template>
  <section class="persona-projection" aria-labelledby="memoir-persona-projection-title">
    <div class="persona-projection__header">
      <div>
        <h3 id="memoir-persona-projection-title" class="persona-projection__title">
          {{ t('memoir.personaProjection.title') }}
        </h3>
        <p class="persona-projection__hint">{{ t('memoir.personaProjection.hint') }}</p>
      </div>
      <UiButton
        variant="ghost"
        size="sm"
        :loading="loading"
        :title="t('memoir.personaProjection.refresh')"
        :aria-label="t('memoir.personaProjection.refresh')"
        @click="loadProjection"
      >
        <ReloadOutlined aria-hidden="true" />
      </UiButton>
    </div>

    <p v-if="errorMsg" class="persona-projection__error">{{ errorMsg }}</p>

    <div v-if="loading && !projection" class="persona-projection__empty">
      {{ t('memoir.personaProjection.loading') }}
    </div>

    <template v-else>
      <div v-if="!projection || projection.empty" class="persona-projection__empty">
        {{ t('memoir.personaProjection.empty') }}
      </div>

      <div v-else class="persona-projection__body">
        <div class="persona-projection__narrative">
          <EyeOutlined class="persona-projection__narrative-icon" aria-hidden="true" />
          <p>
            {{ narrative || t('memoir.personaProjection.narrativePending') }}
          </p>
        </div>

        <div v-if="hasFacts" class="persona-projection__facts">
          <div class="persona-projection__facts-title">
            {{ t('memoir.personaProjection.factsTitle') }}
          </div>
          <ul class="persona-projection__fact-list">
            <li
              v-for="fact in visibleFacts"
              :key="fact.field_id"
              class="persona-projection__fact"
            >
              <span class="persona-projection__fact-label">{{ factLabel(fact) }}</span>
              <span class="persona-projection__fact-value">{{ fact.value }}</span>
              <UiButton
                variant="ghost"
                size="sm"
                :loading="correctingFieldId === fact.field_id"
                @click="correctFact(fact)"
              >
                <EditOutlined aria-hidden="true" />
                <span>{{ t('memoir.personaProjection.correctAction') }}</span>
              </UiButton>
            </li>
          </ul>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.persona-projection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  min-width: 0;
  padding: var(--space-4);
  border: 1px solid rgba(148, 163, 184, 0.18);
  border-radius: 8px;
  background:
    linear-gradient(180deg, rgba(255, 255, 255, 0.05), rgba(255, 255, 255, 0.018)),
    rgba(17, 24, 39, 0.38);
}

.persona-projection__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-3);
}

.persona-projection__title {
  margin: 0;
  font-size: 14px;
  color: var(--color-text);
  letter-spacing: 0;
}

.persona-projection__hint {
  margin: 4px 0 0;
  font-size: var(--font-xs);
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.persona-projection__error {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid rgba(248, 113, 113, 0.3);
  border-radius: 6px;
  background: rgba(248, 113, 113, 0.08);
  color: var(--color-danger, #f87171);
  font-size: var(--font-xs);
}

.persona-projection__empty {
  padding: var(--space-3);
  border: 1px dashed rgba(148, 163, 184, 0.24);
  border-radius: 6px;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  text-align: center;
}

.persona-projection__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.persona-projection__narrative {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: var(--space-2);
  align-items: start;
  padding: var(--space-3);
  border-radius: 6px;
  background: rgba(96, 165, 250, 0.08);
}

.persona-projection__narrative-icon {
  margin-top: 3px;
  color: rgba(147, 197, 253, 0.95);
}

.persona-projection__narrative p {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-sm);
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.persona-projection__facts {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.persona-projection__facts-title {
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
}

.persona-projection__fact-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.persona-projection__fact {
  display: grid;
  grid-template-columns: minmax(72px, auto) minmax(0, 1fr) auto;
  gap: var(--space-2);
  align-items: center;
  padding: 8px 10px;
  border: 1px solid rgba(148, 163, 184, 0.14);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.035);
}

.persona-projection__fact-label {
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
}

.persona-projection__fact-value {
  color: var(--color-text);
  font-size: var(--font-sm);
  line-height: 1.45;
  overflow-wrap: anywhere;
}

@media (max-width: 720px) {
  .persona-projection__fact {
    grid-template-columns: 1fr;
  }
}
</style>
