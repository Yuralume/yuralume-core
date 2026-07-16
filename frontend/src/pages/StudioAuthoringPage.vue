<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'

import ArcTemplateIntakeWizard from '@/components/ArcTemplateIntakeWizard.vue'
import ArcTemplateBeatList from '@/components/ArcTemplateBeatList.vue'
import ArcTemplateLanguageBadge from '@/components/ArcTemplateLanguageBadge.vue'
import { UiButton } from '@/components/ui'
import { useArcTemplateTranslation } from '@/composables/useArcTemplateTranslation'
import { useOperatorLanguage } from '@/composables/useOperatorLanguage'
import {
  bindArcSeriesToCharacter,
  createArcSeries,
  deleteArcSeries,
  draftNextSeason,
  listArcSeries,
  updateArcSeries,
} from '@/utils/api/arcSeries'
import { listArcTemplates } from '@/utils/api/arcTemplates'
import { listCharacters } from '@/utils/api/characters'
import { clampSeedPrompt } from '@/utils/fusionSeed'
import { stashStudioSeed } from '@/utils/studioSeedTransfer'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import type { ArcSeries } from '@/types/arcSeries'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'
import type { ArcTemplate } from '@/types/arcTemplate'
import type { Character } from '@/types/character'

const { t } = useI18n()
const router = useRouter()
const confirmDialog = useConfirmDialog()

interface SeedTemplateCard {
  key: string
  glyph: string
  title: string
  blurb: string
  seedPrompt: string
}

// Seed entry points: start a fusion story from a memory/moment scaffold
// instead of a blank page. Each card carries a self-editable prompt into
// the create form via the shared `?seedPrompt=` seam (no cast — the user
// picks their pair there).
const seedTemplateCards = computed<SeedTemplateCard[]>(() => [
  {
    key: 'whatif',
    glyph: '✦',
    title: t('studio.seedTemplates.whatif.title'),
    blurb: t('studio.seedTemplates.whatif.blurb'),
    seedPrompt: t('studio.seedTemplates.whatif.seed'),
  },
  {
    key: 'daily',
    glyph: '☕',
    title: t('studio.seedTemplates.daily.title'),
    blurb: t('studio.seedTemplates.daily.blurb'),
    seedPrompt: t('studio.seedTemplates.daily.seed'),
  },
  {
    key: 'oneline',
    glyph: '❝',
    title: t('studio.seedTemplates.oneline.title'),
    blurb: t('studio.seedTemplates.oneline.blurb'),
    seedPrompt: t('studio.seedTemplates.oneline.seed'),
  },
])

function startFromSeed(card: SeedTemplateCard) {
  // Canned template text isn't sensitive, but all in-app seed handoffs
  // go through the same in-memory stash — one seam, nothing in the URL.
  stashStudioSeed({ seedPrompt: clampSeedPrompt(card.seedPrompt) })
  void router.push({ name: 'studio-fusion-stories' })
}

const templates = ref<ArcTemplate[]>([])
const series = ref<ArcSeries[]>([])
const characters = ref<Character[]>([])
const loading = ref(false)
const seriesSaving = ref(false)
const seriesDeletingId = ref<string | null>(null)
const seriesBindingId = ref<string | null>(null)
const wizardOpen = ref(false)
const wizardInitialDraft = ref<TemplateDraftPayload | null>(null)
const lastSavedTemplateId = ref<string | null>(null)
const lastSavedSeriesId = ref<string | null>(null)
const continuationDraftingId = ref<string | null>(null)
const editingSeriesId = ref<string | null>(null)
const selectedTemplateIds = ref<string[]>([])
const bindCharacterBySeriesId = reactive<Record<string, string>>({})
const seriesForm = reactive({
  id: '',
  title: '',
  premise: '',
  theme: 'custom',
  tone: 'dramatic',
})
const expandedId = ref<string | null>(null)

function toggleExpand(id: string) {
  expandedId.value = expandedId.value === id ? null : id
}

// Shared "翻成我的語言" mechanism (plan A1 — the same composable the
// player picker uses; kills the studio fork). Read-only: series payloads
// always bind the original template.id (plan D3), only the display view
// is translated.
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

function isTranslateFailed(template: ArcTemplate): boolean {
  return translateEnabled.value && failedIds.value.includes(template.id)
}

const selectedTemplates = computed(() => {
  const byId = new Map(templates.value.map((template) => [template.id, template]))
  return selectedTemplateIds.value
    .map((id) => byId.get(id))
    .filter((template): template is ArcTemplate => Boolean(template))
})

const editingSeries = computed(() => (
  editingSeriesId.value
    ? series.value.find((item) => item.id === editingSeriesId.value) ?? null
    : null
))

const canCreateSeries = computed(() => (
  selectedTemplateIds.value.length >= 2
  && seriesForm.title.trim().length > 0
  && seriesForm.premise.trim().length > 0
))

async function loadStudioData() {
  loading.value = true
  try {
    const [nextTemplates, nextSeries, nextCharacters] = await Promise.all([
      listArcTemplates(),
      listArcSeries(),
      listCharacters(),
    ])
    templates.value = nextTemplates
    series.value = nextSeries
    characters.value = nextCharacters
  } catch (err) {
    notification.error({
      message: t('studio.notifications.loadFailed'),
      description: err instanceof Error ? err.message : t('common.errors.unknown'),
      duration: 4,
    })
  } finally {
    loading.value = false
  }
}

async function handleTemplateSaved(templateId: string) {
  lastSavedTemplateId.value = templateId
  wizardOpen.value = false
  wizardInitialDraft.value = null
  notification.success({
    message: t('studio.notifications.saved', { id: templateId }),
    duration: 3,
  })
  await loadStudioData()
}

function openBlankWizard() {
  wizardInitialDraft.value = null
  wizardOpen.value = true
}

function closeWizard() {
  wizardOpen.value = false
  wizardInitialDraft.value = null
}

function themeLabel(template: ArcTemplate): string {
  return template.theme ? `${template.theme}` : t('common.fallback.notSet')
}

function seriesMemberTitle(memberId: string): string {
  const template = templates.value.find((item) => item.id === memberId)
  return template ? displayTemplate(template).title : memberId
}

function toggleTemplate(templateId: string) {
  const existing = selectedTemplateIds.value.indexOf(templateId)
  if (existing >= 0) {
    selectedTemplateIds.value = selectedTemplateIds.value.filter((id) => id !== templateId)
    return
  }
  selectedTemplateIds.value = [...selectedTemplateIds.value, templateId]
}

function moveSelectedTemplate(index: number, direction: -1 | 1) {
  const nextIndex = index + direction
  if (nextIndex < 0 || nextIndex >= selectedTemplateIds.value.length) return
  const next = [...selectedTemplateIds.value]
  const current = next[index]
  next[index] = next[nextIndex]
  next[nextIndex] = current
  selectedTemplateIds.value = next
}

function resetSeriesForm() {
  editingSeriesId.value = null
  seriesForm.id = ''
  seriesForm.title = ''
  seriesForm.premise = ''
  seriesForm.theme = 'custom'
  seriesForm.tone = 'dramatic'
  selectedTemplateIds.value = []
}

function editSeries(item: ArcSeries) {
  if (item.is_pack) return
  editingSeriesId.value = item.id
  seriesForm.id = item.id
  seriesForm.title = item.title
  seriesForm.premise = item.premise
  seriesForm.theme = item.theme
  seriesForm.tone = item.tone
  selectedTemplateIds.value = item.members
    .slice()
    .sort((a, b) => a.position - b.position)
    .map((member) => member.template_id)
}

async function handleSaveSeries() {
  if (!canCreateSeries.value) {
    notification.warning({
      message: t('studio.series.selectAtLeastTwo'),
      duration: 3,
    })
    return
  }
  seriesSaving.value = true
  try {
    const payload = {
      id: editingSeriesId.value ? null : seriesForm.id.trim() || null,
      title: seriesForm.title.trim(),
      premise: seriesForm.premise.trim(),
      theme: seriesForm.theme.trim() || 'custom',
      tone: seriesForm.tone.trim() || 'dramatic',
      world_frames: [],
      required_traits: [],
      template_ids: selectedTemplateIds.value,
    }
    const saved = editingSeriesId.value
      ? await updateArcSeries(editingSeriesId.value, payload)
      : await createArcSeries(payload)
    lastSavedSeriesId.value = saved.id
    notification.success({
      message: t('studio.notifications.seriesSaved', { id: saved.id }),
      duration: 3,
    })
    resetSeriesForm()
    await loadStudioData()
  } catch (err) {
    notification.error({
      message: t('studio.notifications.seriesSaveFailed'),
      description: err instanceof Error ? err.message : t('common.errors.unknown'),
      duration: 4,
    })
  } finally {
    seriesSaving.value = false
  }
}

async function handleDeleteSeries(item: ArcSeries) {
  if (item.is_pack) return
  if (!await confirmDialog({
    content: t('studio.series.confirmDelete', { title: item.title }),
    okText: t('common.actions.delete'),
    danger: true,
  })) {
    return
  }
  seriesDeletingId.value = item.id
  try {
    await deleteArcSeries(item.id)
    if (editingSeriesId.value === item.id) {
      resetSeriesForm()
    }
    await loadStudioData()
    notification.success({
      message: t('studio.notifications.seriesDeleted', { id: item.id }),
      duration: 3,
    })
  } catch (err) {
    notification.error({
      message: t('studio.notifications.seriesDeleteFailed'),
      description: err instanceof Error ? err.message : t('common.errors.unknown'),
      duration: 4,
    })
  } finally {
    seriesDeletingId.value = null
  }
}

async function handleBindSeries(item: ArcSeries) {
  const characterId = bindCharacterBySeriesId[item.id]
  if (!characterId) {
    notification.warning({
      message: t('studio.series.selectCharacter'),
      duration: 3,
    })
    return
  }
  seriesBindingId.value = item.id
  try {
    await bindArcSeriesToCharacter(item.id, { character_id: characterId })
    await loadStudioData()
    notification.success({
      message: t('studio.notifications.seriesBound'),
      duration: 3,
    })
  } catch (err) {
    notification.error({
      message: t('studio.notifications.seriesBindFailed'),
      description: err instanceof Error ? err.message : t('common.errors.unknown'),
      duration: 4,
    })
  } finally {
    seriesBindingId.value = null
  }
}

async function handleDraftNextSeason(item: ArcSeries) {
  const characterId = bindCharacterBySeriesId[item.id]
  if (!characterId) {
    notification.warning({
      message: t('studio.series.selectCharacter'),
      duration: 3,
    })
    return
  }
  continuationDraftingId.value = item.id
  try {
    const draft = await draftNextSeason(item.id, {
      character_id: characterId,
    })
    wizardInitialDraft.value = draft
    wizardOpen.value = true
    notification.success({
      message: t('studio.notifications.continuationDrafted'),
      duration: 3,
    })
  } catch (err) {
    notification.error({
      message: t('studio.notifications.continuationDraftFailed'),
      description: err instanceof Error ? err.message : t('common.errors.unknown'),
      duration: 4,
    })
  } finally {
    continuationDraftingId.value = null
  }
}

onMounted(() => {
  void loadStudioData()
})
</script>

<template>
  <main class="studio-authoring-page">
    <div class="studio-page__inner">
      <header class="studio-authoring-toolbar">
        <div>
          <h2>{{ t('studio.authoring.title') }}</h2>
          <p>{{ t('studio.authoring.subtitle') }}</p>
        </div>
        <UiButton variant="hero" @click="openBlankWizard">
          {{ t('studio.actions.newTemplate') }}
        </UiButton>
      </header>

      <section class="studio-section" aria-labelledby="studio-seed-title">
        <div class="studio-section__head">
          <div>
            <h2 id="studio-seed-title">{{ t('studio.seedTemplates.title') }}</h2>
            <p>{{ t('studio.seedTemplates.subtitle') }}</p>
          </div>
        </div>
        <ul class="studio-seed__grid" :aria-label="t('studio.seedTemplates.listAria')">
          <li v-for="card in seedTemplateCards" :key="card.key">
            <button
              type="button"
              class="studio-seed__card sheen-hover"
              @click="startFromSeed(card)"
            >
              <span class="studio-seed__mark" aria-hidden="true">{{ card.glyph }}</span>
              <h3 class="display-title">{{ card.title }}</h3>
              <p>{{ card.blurb }}</p>
            </button>
          </li>
        </ul>
      </section>

      <section class="studio-section" aria-labelledby="studio-templates-title">
        <div class="studio-section__head">
          <div>
            <h2 id="studio-templates-title">{{ t('studio.templates.title') }}</h2>
            <p>{{ t('studio.templates.subtitle') }}</p>
          </div>
          <div class="studio-templates__actions">
            <label
              v-if="templates.length"
              class="translate-toggle"
              :title="t('story.arcTemplatePicker.translate.hint')"
            >
              <input
                type="checkbox"
                class="field-checkbox"
                :checked="translateEnabled"
                :disabled="translating"
                @change="toggleTranslate"
              >
              <span>{{ translating ? t('story.arcTemplatePicker.translate.working') : t('story.arcTemplatePicker.translate.label') }}</span>
            </label>
            <UiButton
              v-if="hasFailures"
              variant="ghost"
              size="sm"
              :disabled="translating"
              @click="retryFailed"
            >
              {{ t('story.arcTemplatePicker.translate.retry') }}
            </UiButton>
            <UiButton variant="secondary" size="sm" :loading="loading" @click="loadStudioData">
              {{ t('common.actions.refresh') }}
            </UiButton>
          </div>
        </div>

        <div v-if="loading && templates.length === 0" class="studio-state">
          {{ t('common.state.loading') }}
        </div>
        <div v-else-if="templates.length === 0" class="studio-empty">
          <div class="studio-empty__mark" aria-hidden="true">✦</div>
          <h3 class="display-title">{{ t('studio.templates.emptyTitle') }}</h3>
          <p>{{ t('studio.templates.emptyHint') }}</p>
          <UiButton variant="hero" @click="openBlankWizard">
            {{ t('studio.actions.newTemplate') }}
          </UiButton>
        </div>
        <ul v-else class="template-list" :aria-label="t('studio.templates.listAria')">
          <li
            v-for="template in templates"
            :key="template.id"
            class="template-item sheen-hover"
            :class="{
              'template-item--saved': template.id === lastSavedTemplateId,
              'template-item--expanded': expandedId === template.id,
            }"
          >
            <div class="template-item__main">
              <div class="template-item__title-row">
                <h3 class="display-title">{{ displayTemplate(template).title }}</h3>
                <span class="template-item__meta">
                  {{ t('studio.templates.beatCount', { count: template.beat_count }) }}
                </span>
              </div>
              <p>{{ displayTemplate(template).premise }}</p>
              <div class="template-item__badges">
                <ArcTemplateLanguageBadge :language="displayTemplate(template).language" />
                <span
                  v-if="isTranslateFailed(template)"
                  class="template-item__failed"
                  :title="t('story.arcTemplatePicker.translate.failedHint')"
                >{{ t('story.arcTemplatePicker.translate.failed') }}</span>
              </div>
            </div>
            <div class="template-item__facts">
              <span>{{ t('studio.templates.duration', { days: template.duration_days }) }}</span>
              <span>{{ themeLabel(template) }}</span>
              <span v-if="template.binding.world_frames.length">
                {{ template.binding.world_frames.join(t('common.listSeparator')) }}
              </span>
            </div>
            <button
              type="button"
              class="template-item__toggle"
              :aria-expanded="expandedId === template.id"
              :aria-controls="`studio-tpl-beats-${template.id}`"
              @click="toggleExpand(template.id)"
            >
              {{ expandedId === template.id ? t('story.arcTemplatePicker.collapseBeats') : t('story.arcTemplatePicker.expandBeats') }}
            </button>
            <div
              v-if="expandedId === template.id"
              :id="`studio-tpl-beats-${template.id}`"
              class="template-item__beats"
            >
              <ArcTemplateBeatList :beats="displayTemplate(template).beats" />
            </div>
          </li>
        </ul>
      </section>

      <section class="studio-grid" :aria-label="t('studio.authoring.ariaLabel')">
        <form class="series-form" @submit.prevent="handleSaveSeries">
          <div class="studio-section__head">
            <div>
              <p class="spark-label">{{ t('studio.series.formTitle') }}</p>
              <h2>
                {{ editingSeries ? t('studio.series.editFormTitle') : t('studio.series.formTitle') }}
              </h2>
              <p>{{ t('studio.series.formHint') }}</p>
            </div>
          </div>

          <div class="series-form__fields">
            <p class="spark-label series-form__group-label">{{ t('studio.series.formTitle') }}</p>
            <label class="field-label">
              {{ t('studio.series.idLabel') }}
              <input
                v-model="seriesForm.id"
                class="field-input"
                :placeholder="t('studio.series.idPlaceholder')"
                maxlength="64"
                :disabled="Boolean(editingSeriesId)"
              >
            </label>
            <label class="field-label">
              {{ t('studio.series.titleLabel') }}
              <input
                v-model="seriesForm.title"
                class="field-input"
                :placeholder="t('studio.series.titlePlaceholder')"
                required
              >
            </label>
            <label class="field-label series-form__full">
              {{ t('studio.series.premiseLabel') }}
              <textarea
                v-model="seriesForm.premise"
                class="field-textarea"
                rows="3"
                :placeholder="t('studio.series.premisePlaceholder')"
                required
              />
            </label>
            <label class="field-label">
              {{ t('studio.series.themeLabel') }}
              <input v-model="seriesForm.theme" class="field-input">
            </label>
            <label class="field-label">
              {{ t('studio.series.toneLabel') }}
              <input v-model="seriesForm.tone" class="field-input">
            </label>
          </div>

          <div class="series-picker">
            <div class="series-picker__head">
              <span class="spark-label">{{ t('studio.series.templatesLabel') }}</span>
              <span>{{ t('studio.series.memberCount', { count: selectedTemplateIds.length }) }}</span>
            </div>
            <div v-if="templates.length === 0" class="studio-state">
              {{ t('studio.series.noTemplates') }}
            </div>
            <div v-else class="series-picker__options">
              <label
                v-for="template in templates"
                :key="template.id"
                class="series-option"
                :class="{ 'series-option--selected': selectedTemplateIds.includes(template.id) }"
              >
                <input
                  type="checkbox"
                  class="field-checkbox"
                  :checked="selectedTemplateIds.includes(template.id)"
                  @change="toggleTemplate(template.id)"
                >
                <span>
                  <strong>{{ displayTemplate(template).title }}</strong>
                  <small>{{ template.id }}</small>
                </span>
              </label>
            </div>
          </div>

          <div class="series-order">
            <div class="series-picker__head">
              <span class="spark-label">{{ t('studio.series.selectedOrder') }}</span>
              <span v-if="selectedTemplateIds.length < 2">{{ t('studio.series.selectAtLeastTwo') }}</span>
            </div>
            <ol v-if="selectedTemplates.length" class="series-order__list">
              <li v-for="(template, index) in selectedTemplates" :key="template.id">
                <span>{{ displayTemplate(template).title }}</span>
                <div class="series-order__actions">
                  <button
                    type="button"
                    :disabled="index === 0"
                    :aria-label="t('studio.series.moveUp', { title: displayTemplate(template).title })"
                    @click="moveSelectedTemplate(index, -1)"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    :disabled="index === selectedTemplates.length - 1"
                    :aria-label="t('studio.series.moveDown', { title: displayTemplate(template).title })"
                    @click="moveSelectedTemplate(index, 1)"
                  >
                    ↓
                  </button>
                </div>
              </li>
            </ol>
          </div>

          <div class="series-form__actions">
            <UiButton variant="secondary" type="button" @click="resetSeriesForm">
              {{ t('common.actions.cancel') }}
            </UiButton>
            <UiButton
              variant="hero"
              type="submit"
              :disabled="!canCreateSeries"
              :loading="seriesSaving"
            >
              {{ editingSeries ? t('studio.actions.updateSeries') : t('studio.actions.createSeries') }}
            </UiButton>
          </div>
        </form>

        <section class="studio-section" aria-labelledby="studio-series-title">
          <div class="studio-section__head">
            <div>
              <h2 id="studio-series-title">{{ t('studio.series.title') }}</h2>
              <p>{{ t('studio.series.subtitle') }}</p>
            </div>
          </div>

          <div v-if="loading && series.length === 0" class="studio-state">
            {{ t('common.state.loading') }}
          </div>
          <div v-else-if="series.length === 0" class="studio-empty">
            <div class="studio-empty__mark" aria-hidden="true">✧</div>
            <h3 class="display-title">{{ t('studio.series.emptyTitle') }}</h3>
            <p>{{ t('studio.series.emptyHint') }}</p>
          </div>
          <ul v-else class="series-list" :aria-label="t('studio.series.listAria')">
            <li
              v-for="item in series"
              :key="item.id"
              class="series-item sheen-hover"
              :class="{ 'series-item--saved': item.id === lastSavedSeriesId }"
            >
              <div class="series-item__head">
                <div>
                  <h3 class="display-title">{{ item.title }}</h3>
                  <p>{{ item.premise }}</p>
                </div>
                <div class="series-item__tools">
                  <span class="template-item__meta">
                    {{ item.is_pack ? t('studio.series.packLabel') : t('studio.series.authoredLabel') }}
                  </span>
                  <UiButton
                    v-if="!item.is_pack"
                    variant="ghost"
                    size="sm"
                    type="button"
                    @click="editSeries(item)"
                  >
                    {{ t('common.actions.edit') }}
                  </UiButton>
                  <UiButton
                    v-if="!item.is_pack"
                    variant="danger"
                    size="sm"
                    type="button"
                    :loading="seriesDeletingId === item.id"
                    @click="handleDeleteSeries(item)"
                  >
                    {{ t('common.actions.delete') }}
                  </UiButton>
                </div>
              </div>
              <div class="template-item__facts">
                <span>{{ t('studio.series.memberCount', { count: item.member_count }) }}</span>
                <span>{{ item.theme }}</span>
                <span>{{ item.tone }}</span>
              </div>
              <div class="series-item__members">
                <strong>{{ t('studio.series.membersTitle') }}</strong>
                <ol>
                  <li
                    v-for="member in item.members"
                    :key="`${item.id}-${member.template_id}`"
                    class="series-track__item"
                  >
                    <span class="series-track__node">{{ member.position + 1 }}</span>
                    <span>{{ seriesMemberTitle(member.template_id) }}</span>
                  </li>
                </ol>
              </div>
              <div class="series-bind">
                <label class="field-label">
                  {{ t('studio.series.bindCharacterLabel') }}
                  <select
                    v-model="bindCharacterBySeriesId[item.id]"
                    class="field-select"
                    :disabled="characters.length === 0"
                  >
                    <option value="">{{ t('studio.series.bindCharacterPlaceholder') }}</option>
                    <option
                      v-for="character in characters"
                      :key="character.id"
                      :value="character.id"
                    >
                      {{ character.name }}
                    </option>
                  </select>
                </label>
                <UiButton
                  variant="secondary"
                  type="button"
                  :disabled="!bindCharacterBySeriesId[item.id]"
                  :loading="seriesBindingId === item.id"
                  @click="handleBindSeries(item)"
                >
                  {{ t('studio.series.bindAction') }}
                </UiButton>
                <UiButton
                  variant="hero"
                  type="button"
                  :disabled="!bindCharacterBySeriesId[item.id]"
                  :loading="continuationDraftingId === item.id"
                  @click="handleDraftNextSeason(item)"
                >
                  {{ t('studio.series.draftNextSeason') }}
                </UiButton>
              </div>
            </li>
          </ul>
        </section>
      </section>

      <section class="studio-section studio-section--compact" aria-labelledby="studio-library-title">
        <div class="studio-section__head">
          <div>
            <h2 id="studio-library-title">{{ t('studio.library.title') }}</h2>
            <p>{{ t('studio.library.subtitle') }}</p>
          </div>
        </div>
        <div class="studio-state">{{ t('studio.library.hint') }}</div>
      </section>
    </div>

    <ArcTemplateIntakeWizard
      v-if="wizardOpen"
      :initial-draft="wizardInitialDraft"
      @saved="handleTemplateSaved"
      @close="closeWizard"
    />
  </main>
</template>

<style scoped>
.studio-authoring-page {
  color: var(--color-text);
}

.studio-page__inner {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.studio-authoring-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
}

.studio-authoring-toolbar > div {
  min-width: 0;
}

.studio-authoring-toolbar h2,
.studio-section h2,
.studio-empty h3,
.template-item h3 {
  margin: 0;
  letter-spacing: 0;
}

.studio-authoring-toolbar h2 {
  font-size: 32px;
  font-family: var(--font-display);
  font-weight: 700;
}

.studio-authoring-toolbar p,
.studio-section p,
.studio-empty p,
.template-item p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.studio-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.studio-grid {
  display: grid;
  grid-template-columns: minmax(320px, 0.92fr) minmax(360px, 1fr);
  gap: var(--space-4);
  align-items: start;
}

.studio-section__head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-3);
}

.studio-section h2 {
  font-size: var(--font-lg);
  font-weight: 650;
}

.studio-state,
.studio-empty {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.04);
}

.studio-state {
  padding: var(--space-4);
  color: var(--color-text-secondary);
}

.studio-empty {
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-2);
  position: relative;
  overflow: hidden;
  background:
    radial-gradient(180px 120px at 24px 18px, rgba(var(--color-primary-rgb), 0.2), transparent 72%),
    radial-gradient(circle, rgba(255, 255, 255, 0.14) 0 1px, transparent 1px),
    rgba(255, 255, 255, 0.04);
  background-size: auto, 38px 38px, auto;
}

.studio-empty__mark {
  width: 46px;
  height: 46px;
  border-radius: 999px;
  color: var(--color-spark);
  background: rgba(var(--color-primary-rgb), 0.14);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 0 28px rgba(var(--color-primary-rgb), 0.24);
}

.studio-empty h3 {
  font-size: 28px;
}

.studio-seed__grid {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-3);
}

.studio-seed__card {
  width: 100%;
  min-height: 150px;
  text-align: left;
  cursor: pointer;
  padding: var(--space-3);
  border: 1px solid transparent;
  border-radius: 8px;
  background:
    linear-gradient(rgba(18, 12, 42, 0.86), rgba(18, 12, 42, 0.86)) padding-box,
    linear-gradient(135deg, rgba(var(--color-spark-rgb), 0.34), rgba(var(--color-primary-rgb), 0.48), rgba(var(--color-secondary-rgb), 0.22)) border-box;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  color: var(--color-text);
  transition: transform 0.16s ease, box-shadow 0.16s ease;
}

.studio-seed__card:hover {
  transform: translateY(-2px);
  box-shadow:
    0 12px 28px rgba(0, 0, 0, 0.22),
    0 0 24px rgba(var(--color-primary-rgb), 0.18);
}

.studio-seed__card:focus-visible {
  outline: 2px solid var(--color-spark);
  outline-offset: 2px;
}

.studio-seed__mark {
  width: 40px;
  height: 40px;
  border-radius: 999px;
  color: var(--color-spark);
  background: rgba(var(--color-primary-rgb), 0.14);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  box-shadow: 0 0 22px rgba(var(--color-primary-rgb), 0.22);
}

.studio-seed__card h3 {
  margin: 0;
  font-size: 20px;
}

.studio-seed__card p {
  margin: 0;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.template-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.template-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--space-3);
}

.template-item {
  min-width: 0;
  min-height: 180px;
  padding: var(--space-3);
  border: 1px solid transparent;
  border-radius: 8px;
  background:
    linear-gradient(rgba(18, 12, 42, 0.86), rgba(18, 12, 42, 0.86)) padding-box,
    linear-gradient(135deg, rgba(var(--color-spark-rgb), 0.34), rgba(var(--color-primary-rgb), 0.48), rgba(var(--color-secondary-rgb), 0.22)) border-box;
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
  gap: var(--space-3);
  transition: transform 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
}

.studio-templates__actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.translate-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
}

.translate-toggle input {
  accent-color: var(--color-spark);
  cursor: pointer;
}

.template-item__badges {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.template-item__failed {
  font-size: 10px;
  padding: 2px 7px;
  border-radius: 999px;
  font-weight: 600;
  background: rgba(231, 175, 60, 0.2);
  color: #f0c87a;
}

.template-item__toggle {
  align-self: flex-start;
  background: none;
  border: none;
  padding: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.template-item__toggle:hover {
  color: var(--color-text);
}

.template-item__beats {
  padding-top: var(--space-2);
  border-top: 1px dashed var(--color-border);
}

.template-item:hover {
  transform: translateY(-2px);
  box-shadow:
    0 12px 28px rgba(0, 0, 0, 0.22),
    0 0 24px rgba(var(--color-primary-rgb), 0.18);
}

.template-item--saved {
  background:
    linear-gradient(rgba(18, 12, 42, 0.86), rgba(18, 12, 42, 0.86)) padding-box,
    linear-gradient(135deg, rgba(var(--color-spark-rgb), 0.78), rgba(var(--color-primary-rgb), 0.62)) border-box;
  box-shadow: 0 0 26px rgba(var(--color-spark-rgb), 0.18);
}

.template-item__main {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.template-item__title-row {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  align-items: flex-start;
}

.template-item h3 {
  min-width: 0;
  font-size: 22px;
  overflow-wrap: anywhere;
}

.template-item__meta {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(var(--color-spark-rgb), 0.14);
  color: var(--color-spark);
  font-size: var(--font-xs);
  border: 1px solid rgba(var(--color-spark-rgb), 0.24);
}

.template-item p {
  display: -webkit-box;
  overflow: hidden;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
}

.template-item__facts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.template-item__facts span {
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(var(--color-secondary-rgb), 0.12);
  color: var(--color-secondary-light);
  font-size: var(--font-xs);
  overflow-wrap: anywhere;
}

.series-form,
.series-item {
  border: 1px solid rgba(var(--color-primary-rgb), 0.18);
  border-radius: 8px;
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.08), rgba(255, 255, 255, 0.025)),
    rgba(18, 12, 42, 0.48);
}

.series-form {
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.series-form__fields {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: var(--space-3);
  padding: var(--space-3);
  border: 1px solid rgba(var(--color-primary-rgb), 0.14);
  border-radius: 8px;
  background: rgba(0, 0, 0, 0.12);
}

.series-form__full {
  grid-column: 1 / -1;
}

.series-form__group-label {
  grid-column: 1 / -1;
  margin: 0;
}

.series-picker,
.series-order {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.series-picker__head {
  display: flex;
  justify-content: space-between;
  gap: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
}

.series-picker__options {
  display: grid;
  gap: var(--space-2);
  max-height: 260px;
  overflow-y: auto;
}

.series-option {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  cursor: pointer;
}

.series-option--selected {
  border-color: rgba(var(--color-spark-rgb), 0.58);
  background:
    linear-gradient(145deg, rgba(var(--color-primary-rgb), 0.16), rgba(var(--color-spark-rgb), 0.06)),
    rgba(255, 255, 255, 0.03);
}

.series-option span {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.series-option strong,
.series-item h3 {
  overflow-wrap: anywhere;
}

.series-option small {
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
}

.series-order__list,
.series-item__members ol,
.series-list {
  margin: 0;
}

.series-order__list {
  padding-left: 1.4rem;
  display: grid;
  gap: var(--space-2);
}

.series-order__list li {
  padding: 6px 8px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.18);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.series-order__actions {
  display: flex;
  gap: 4px;
}

.series-order__actions button {
  width: 28px;
  height: 28px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text);
  font-size: 0;
}

.series-order__actions button:disabled {
  opacity: 0.35;
}

.series-order__actions button::before {
  font-size: var(--font-sm);
  line-height: 1;
}

.series-order__actions button:first-child::before {
  content: '\2191';
}

.series-order__actions button:last-child::before {
  content: '\2193';
}

.series-form__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}

.series-list {
  padding: 0;
  list-style: none;
  display: grid;
  gap: var(--space-3);
}

.series-item {
  padding: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease;
}

.series-item:hover {
  transform: translateY(-1px);
  border-color: rgba(var(--color-primary-rgb), 0.42);
  box-shadow: 0 0 24px rgba(var(--color-primary-rgb), 0.16);
}

.series-item--saved {
  border-color: rgba(var(--color-spark-rgb), 0.6);
  box-shadow: 0 0 24px rgba(var(--color-spark-rgb), 0.16);
}

.series-item__head {
  display: flex;
  justify-content: space-between;
  gap: var(--space-3);
  align-items: flex-start;
}

.series-item__tools {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.series-item h3,
.series-item p {
  margin: 0;
}

.series-item h3 {
  font-size: 24px;
}

.series-item p {
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.series-item__members {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
}

.series-item__members ol {
  position: relative;
  padding-left: 0;
  list-style: none;
  display: grid;
  gap: var(--space-2);
}

.series-item__members ol::before {
  content: "";
  position: absolute;
  left: 13px;
  top: 16px;
  bottom: 16px;
  width: 1px;
  background: linear-gradient(rgba(var(--color-spark-rgb), 0.5), rgba(var(--color-primary-rgb), 0.24));
}

.series-track__item {
  position: relative;
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  align-items: center;
  gap: var(--space-2);
  min-height: 32px;
}

.series-track__node {
  position: relative;
  z-index: 1;
  width: 28px;
  height: 28px;
  border-radius: 999px;
  background: rgba(var(--color-spark-rgb), 0.16);
  color: var(--color-spark);
  border: 1px solid rgba(var(--color-spark-rgb), 0.42);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: var(--font-xs);
  font-weight: 700;
}

.series-bind {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: end;
  gap: var(--space-2);
}

.studio-section--compact {
  padding-top: var(--space-2);
}

@media (max-width: 720px) {
  .studio-section__head {
    align-items: flex-start;
    flex-direction: column;
  }

  .template-list,
  .studio-seed__grid {
    grid-template-columns: 1fr;
  }

  .studio-grid,
  .series-form__fields {
    grid-template-columns: 1fr;
  }

  .series-item__head,
  .series-form__actions,
  .studio-authoring-toolbar,
  .series-item__tools {
    align-items: stretch;
    flex-direction: column;
  }

  .series-bind {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .template-item,
  .template-item:hover,
  .studio-seed__card,
  .studio-seed__card:hover,
  .series-item,
  .series-item:hover {
    transform: none;
    transition: none;
  }
}
</style>
