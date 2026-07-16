<script setup lang="ts">
/**
 * 劇情骨架選擇器（Phase 2.5 of SCENE_BEAT_PLAN）。
 *
 * Modal 形式：列出後端 bundled YAML templates，可展開查看完整 beat 結構，
 * 然後綁定到 character（或解除綁定回到 LLM 即興規劃）。
 *
 * 此元件**只負責挑選**——綁定後是否要立刻開新 arc 由父層決定。
 *
 * Emits:
 * - ``select(template_id)`` — 使用者選擇某 template；父層應 PATCH
 *   character.arc_template_id = template_id
 * - ``clear()`` — 使用者選「不使用範本」；父層應 PATCH
 *   character.arc_template_id = null
 * - ``close()`` — 取消／關閉 modal
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ArcTemplate } from '@/types/arcTemplate'
import { listArcTemplates } from '@/utils/api/arcTemplates'
import { useArcTemplateTranslation } from '@/composables/useArcTemplateTranslation'
import { useOperatorLanguage } from '@/composables/useOperatorLanguage'
import ArcTemplateBeatList from './ArcTemplateBeatList.vue'
import ArcTemplateLanguageBadge from './ArcTemplateLanguageBadge.vue'
import ArcTemplateIntakeWizard from './ArcTemplateIntakeWizard.vue'

const props = defineProps<{
  /**
   * 目前 character 綁定的 template id；用來在卡片上標記「目前使用中」。
   */
  currentTemplateId: string | null
  characterId?: string | null
  /**
   * Character 的 ``world_frame`` —— 用來在不相容的 template 卡片上加
   * 「世界觀不符」warning，但不阻擋選擇（綁定不夠合適的 template 仍是
   * 操作者的選擇權）。
   */
  worldFrame: string | null
}>()

const emit = defineEmits<{
  (e: 'select', templateId: string): void
  (e: 'clear'): void
  (e: 'close'): void
}>()

const { t } = useI18n()

const templates = ref<ArcTemplate[]>([])
const loading = ref(false)
const errorMsg = ref<string | null>(null)
const expandedId = ref<string | null>(null)
const wizardOpen = ref(false)

// "翻成我的語言" opt-in — shared with Creator Studio via a single
// composable (plan D1: kill the fork). Translations are read-only and
// never persisted; a failure falls back to authored prose and can retry.
const { targetLanguage } = useOperatorLanguage()
const {
  translateEnabled,
  translating,
  hasFailures,
  failedIds,
  displayTemplate,
  toggleTranslate,
  retryFailed,
} = useArcTemplateTranslation(templates, {
  targetLanguage,
  persistKey: 'yuralume.arcTemplates.translateToMyLanguage',
})

function isTranslateFailed(tpl: ArcTemplate): boolean {
  return translateEnabled.value && failedIds.value.includes(tpl.id)
}

async function reload() {
  loading.value = true
  errorMsg.value = null
  try {
    templates.value = await listArcTemplates({
      characterId: props.characterId ?? null,
    })
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('story.arcTemplatePicker.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  reload()
})

function toggleExpand(id: string) {
  expandedId.value = expandedId.value === id ? null : id
}

function isCompatible(tpl: ArcTemplate): boolean {
  if (!props.worldFrame) return true
  if (tpl.binding.world_frames.length === 0) return true
  return tpl.binding.world_frames.includes(props.worldFrame)
}

function selectTemplate(id: string) {
  emit('select', id)
}

function clearBinding() {
  emit('clear')
}

function close() {
  emit('close')
}

function openWizard() {
  wizardOpen.value = true
}

async function onWizardSaved(templateId: string) {
  wizardOpen.value = false
  // 重新撈一次列表把新範本拉進來，並自動展開
  await reload()
  expandedId.value = templateId
  // 立即套用：跟一般卡片點「使用此範本」一致
  emit('select', templateId)
}

const sortedTemplates = computed<ArcTemplate[]>(() => {
  // Compatible templates first, then by id (stable visual order so
  // operator's eye position doesn't shift between reloads).
  return [...templates.value].sort((a, b) => {
    const aOk = isCompatible(a) ? 0 : 1
    const bOk = isCompatible(b) ? 0 : 1
    if (aOk !== bOk) return aOk - bOk
    return a.id.localeCompare(b.id)
  })
})
</script>

<template>
  <Teleport to="body">
    <div class="modal-backdrop" @click.self="close">
      <div class="picker" role="dialog" :aria-label="t('story.arcTemplatePicker.ariaLabel')">
        <div class="picker-header">
          <div>
            <div class="picker-title display-title">{{ t('story.arcTemplatePicker.title') }}</div>
            <div class="picker-hint">
              {{ t('story.arcTemplatePicker.hint') }}
            </div>
          </div>
          <div class="header-actions">
            <label class="translate-toggle" :title="t('story.arcTemplatePicker.translate.hint')">
              <input
                type="checkbox"
                :checked="translateEnabled"
                :disabled="translating"
                @change="toggleTranslate"
              />
              <span>{{ translating ? t('story.arcTemplatePicker.translate.working') : t('story.arcTemplatePicker.translate.label') }}</span>
            </label>
            <button
              v-if="hasFailures"
              type="button"
              class="chip-btn translate-retry"
              :disabled="translating"
              :title="t('story.arcTemplatePicker.translate.retryHint')"
              @click="retryFailed"
            >{{ t('story.arcTemplatePicker.translate.retry') }}</button>
            <button
              class="chip-btn alt new-tpl-btn"
              type="button"
              @click="openWizard"
            >{{ t('story.arcTemplatePicker.newTemplate') }}</button>
            <button class="close-btn" @click="close" :aria-label="t('common.actions.close')">×</button>
          </div>
        </div>

        <div class="picker-body">
          <div v-if="loading" class="empty-msg">{{ t('common.state.loading') }}</div>
          <div v-else-if="errorMsg" class="error-msg">{{ errorMsg }}</div>
          <div v-else-if="sortedTemplates.length === 0" class="empty-msg">
            {{ t('story.arcTemplatePicker.emptyPrefix') }}
            <code>data/arc_templates/</code>
            {{ t('story.arcTemplatePicker.emptySuffix') }}
          </div>
          <ul v-else class="tpl-list">
            <li
              v-for="tpl in sortedTemplates"
              :key="tpl.id"
              :class="[
                'tpl-card',
                { current: tpl.id === currentTemplateId },
                { incompatible: !isCompatible(tpl) },
                { expanded: expandedId === tpl.id },
              ]"
            >
              <div class="tpl-summary" @click="toggleExpand(tpl.id)">
                <div class="tpl-head">
                  <span class="tpl-title">{{ displayTemplate(tpl).title }}</span>
                  <span
                    v-if="tpl.id === currentTemplateId"
                    class="pill pill-current"
                  >{{ t('story.arcTemplatePicker.current') }}</span>
                  <ArcTemplateLanguageBadge :language="displayTemplate(tpl).language" />
                  <span
                    v-if="isTranslateFailed(tpl)"
                    class="pill pill-warn"
                    :title="t('story.arcTemplatePicker.translate.failedHint')"
                  >{{ t('story.arcTemplatePicker.translate.failed') }}</span>
                  <span
                    v-if="!isCompatible(tpl)"
                    class="pill pill-warn"
                    :title="t('story.arcTemplatePicker.incompatibleTitle', {
                      frames: tpl.binding.world_frames.join('/'),
                      worldFrame,
                    })"
                  >{{ t('story.arcTemplatePicker.incompatible') }}</span>
                </div>
                <div class="tpl-meta">
                  <span class="meta-pill theme">{{ tpl.theme }}</span>
                  <span class="meta-pill">{{ t('story.arcTemplatePicker.durationDays', { count: tpl.duration_days }) }}</span>
                  <span class="meta-pill">{{ tpl.beat_count }} beats</span>
                  <span
                    v-for="frame in tpl.binding.world_frames"
                    :key="frame"
                    class="meta-pill frame"
                  >{{ frame }}</span>
                </div>
                <div class="tpl-premise">{{ displayTemplate(tpl).premise }}</div>
                <button
                  type="button"
                  class="tpl-toggle"
                  :aria-expanded="expandedId === tpl.id"
                  :aria-controls="`arc-tpl-beats-${tpl.id}`"
                  @click.stop="toggleExpand(tpl.id)"
                >
                  {{ expandedId === tpl.id ? t('story.arcTemplatePicker.collapseBeats') : t('story.arcTemplatePicker.expandBeats') }}
                </button>
              </div>

              <div
                v-if="expandedId === tpl.id"
                :id="`arc-tpl-beats-${tpl.id}`"
                class="tpl-beats-outer"
              >
                <ArcTemplateBeatList :beats="displayTemplate(tpl).beats" />
              </div>

              <div class="tpl-actions">
                <button
                  class="chip-btn primary"
                  :disabled="tpl.id === currentTemplateId"
                  @click="selectTemplate(tpl.id)"
                >{{ tpl.id === currentTemplateId ? t('story.arcTemplatePicker.bound') : t('story.arcTemplatePicker.useTemplate') }}</button>
              </div>
            </li>
          </ul>
        </div>

        <div class="picker-footer">
          <button
            class="chip-btn"
            :disabled="!currentTemplateId"
            @click="clearBinding"
          >{{ t('story.arcTemplatePicker.clearBinding') }}</button>
          <button class="chip-btn" @click="close">{{ t('common.actions.close') }}</button>
        </div>
      </div>
    </div>

    <ArcTemplateIntakeWizard
      v-if="wizardOpen"
      :target-character-id="characterId ?? null"
      @saved="onWizardSaved"
      @close="wizardOpen = false"
    />
  </Teleport>
</template>

<style scoped>
.modal-backdrop {
  position: fixed;
  inset: 0;
  z-index: 1300;
  background: rgba(0, 0, 0, 0.78);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
}

.picker {
  width: min(720px, 100%);
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.08), rgba(255, 255, 255, 0.025)),
    var(--color-surface);
  border: 1px solid rgba(var(--color-primary-rgb), 0.24);
  border-radius: 10px;
  overflow: hidden;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.46);
}

.picker-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
  gap: 12px;
}

.picker-title {
  font-size: 26px;
  color: var(--color-text);
  margin-bottom: 4px;
}

.picker-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.new-tpl-btn {
  font-size: 11px;
}

.close-btn {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  font-size: 20px;
  cursor: pointer;
  line-height: 1;
  padding: 0 4px;
}

.chip-btn.alt {
  background: rgba(var(--color-secondary-rgb), 0.15);
  color: #8ac8e8;
  border-color: rgba(var(--color-secondary-rgb), 0.4);
}

.chip-btn.alt:hover:not(:disabled) {
  background: rgba(var(--color-secondary-rgb), 0.25);
}

.close-btn:hover {
  color: var(--color-text);
}

.picker-body {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 14px;
}

.empty-msg,
.error-msg {
  padding: 14px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.error-msg {
  color: #ff8a75;
}

.tpl-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.tpl-card {
  border: 1px solid rgba(var(--color-primary-rgb), 0.16);
  border-radius: 8px;
  background:
    linear-gradient(145deg, rgba(255, 255, 255, 0.045), rgba(255, 255, 255, 0.018)),
    rgba(18, 12, 42, 0.44);
  display: flex;
  flex-direction: column;
  transition: border-color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}

.tpl-card.current {
  border-color: rgba(var(--color-spark-rgb), 0.62);
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.16), rgba(var(--color-spark-rgb), 0.06)),
    rgba(18, 12, 42, 0.58);
  box-shadow:
    0 0 0 1px rgba(var(--color-spark-rgb), 0.12) inset,
    0 0 24px rgba(var(--color-spark-rgb), 0.14);
}

.tpl-card:hover {
  transform: translateY(-1px);
  border-color: rgba(var(--color-primary-rgb), 0.42);
}

.tpl-card.incompatible {
  opacity: 0.7;
}

.tpl-summary {
  padding: 12px 14px;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tpl-summary:hover {
  background: rgba(255, 255, 255, 0.04);
}

.tpl-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.tpl-title {
  font-family: var(--font-display);
  font-size: 22px;
  font-weight: 700;
  color: var(--color-text);
}

.pill {
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 999px;
  font-weight: 600;
}

.pill-current {
  background: rgba(var(--color-spark-rgb), 0.16);
  color: var(--color-spark);
  border: 1px solid rgba(var(--color-spark-rgb), 0.32);
}

.pill-warn {
  background: rgba(231, 175, 60, 0.2);
  color: #f0c87a;
}

.translate-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--color-text-muted, #94a3b8);
  cursor: pointer;
  user-select: none;
}

.translate-toggle input {
  accent-color: var(--color-spark);
  cursor: pointer;
}

.tpl-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.meta-pill {
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.07);
  color: var(--color-text-secondary);
}

.meta-pill.theme {
  background: rgba(var(--color-primary-rgb), 0.16);
  color: var(--color-primary-light);
  border: 1px solid rgba(var(--color-primary-rgb), 0.24);
}

.meta-pill.frame {
  background: rgba(var(--color-secondary-rgb), 0.15);
  color: var(--color-secondary-light);
  border: 1px solid rgba(var(--color-secondary-rgb), 0.22);
}

.tpl-premise {
  font-size: 12px;
  color: var(--color-text);
  line-height: 1.5;
}

.tpl-toggle {
  align-self: flex-start;
  background: none;
  border: none;
  padding: 0;
  font-size: 11px;
  color: var(--color-text-secondary);
  cursor: pointer;
}

.tpl-toggle:hover {
  color: var(--color-text);
}

.translate-retry {
  font-size: 10px;
}

.tpl-beats-outer {
  padding: 10px 14px 12px;
  border-top: 1px dashed var(--color-border);
}

.tpl-actions {
  padding: 8px 14px 12px;
  display: flex;
  justify-content: flex-end;
  gap: 6px;
}

.picker-footer {
  padding: 12px 18px;
  border-top: 1px solid var(--color-border);
  background: rgba(0, 0, 0, 0.2);
  display: flex;
  justify-content: space-between;
  gap: 6px;
  flex-wrap: wrap;
}

.chip-btn {
  padding: 6px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  border-radius: 999px;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.15s, color 0.15s;
}

.chip-btn:hover:not(:disabled) {
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text);
}

.chip-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.chip-btn.primary {
  background: var(--grad-flame);
  color: white;
  border-color: rgba(var(--color-primary-rgb), 0.58);
  box-shadow: 0 8px 24px rgba(var(--color-primary-rgb), 0.22);
}

.chip-btn.primary:hover:not(:disabled) {
  filter: brightness(1.08);
}

@media (max-width: 600px) {
  .modal-backdrop {
    padding: 12px;
  }

  .picker {
    max-height: calc(100vh - 24px);
    border-radius: 8px;
  }

  .picker-header {
    padding: 12px 16px;
  }

  .picker-footer {
    padding: 10px 16px;
  }
}

@media (prefers-reduced-motion: reduce) {
  .tpl-card,
  .tpl-card:hover {
    transform: none;
    transition: none;
  }
}
</style>
