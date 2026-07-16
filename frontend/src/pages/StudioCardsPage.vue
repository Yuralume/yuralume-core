<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'

import { UiButton } from '@/components/ui'
import CharacterCardFace from '@/components/CharacterCardFace.vue'
import { listArcSeries } from '@/utils/api/arcSeries'
import { downloadCharacterCard, listCharacters } from '@/utils/api/characters'
import type { CharacterCardPreview } from '@/utils/api/characters'
import type { ArcSeries } from '@/types/arcSeries'
import type { Character } from '@/types/character'

const { t } = useI18n()

const characters = ref<Character[]>([])
const series = ref<ArcSeries[]>([])
const selectedCharacterId = ref('')
const selectedSeriesIds = ref<string[]>([])
const loading = ref(false)
const error = ref('')

const selectedCharacter = computed(() => (
  characters.value.find((character) => character.id === selectedCharacterId.value)
  ?? null
))

const selectedSeries = computed(() => (
  selectedSeriesIds.value
    .map((id) => series.value.find((item) => item.id === id))
    .filter((item): item is ArcSeries => Boolean(item))
))

const previewCard = computed<CharacterCardPreview>(() => {
  const character = selectedCharacter.value
  const bundledSeries = selectedSeries.value
  if (!character) {
    return buildEmptyPreview(bundledSeries)
  }
  return buildCharacterPreview(character, bundledSeries)
})

const exportDisabled = computed(() => !selectedCharacterId.value || loading.value)

onMounted(load)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [loadedCharacters, loadedSeries] = await Promise.all([
      listCharacters(),
      listArcSeries(),
    ])
    characters.value = loadedCharacters
    series.value = loadedSeries
    selectedCharacterId.value = loadedCharacters[0]?.id ?? ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
  } finally {
    loading.value = false
  }
}

function toggleSeries(seriesId: string, checked: boolean) {
  if (checked) {
    if (!selectedSeriesIds.value.includes(seriesId)) {
      selectedSeriesIds.value = [...selectedSeriesIds.value, seriesId]
    }
    return
  }
  selectedSeriesIds.value = selectedSeriesIds.value.filter((id) => id !== seriesId)
}

function handleSeriesChange(seriesId: string, event: Event) {
  toggleSeries(seriesId, (event.target as HTMLInputElement).checked)
}

function exportCard() {
  if (!selectedCharacterId.value) return
  downloadCharacterCard(selectedCharacterId.value, {
    includeArcSeriesIds: selectedSeriesIds.value,
  })
}

function buildEmptyPreview(bundledSeries: ArcSeries[]): CharacterCardPreview {
  return {
    pack_id: null,
    title: t('studio.cards.previewEmptyTitle'),
    author: 'Yuralume',
    description: t('studio.cards.previewEmptyDescription'),
    tags: [],
    note: t('studio.cards.previewEmptyHint'),
    name: t('studio.cards.previewEmptyName'),
    summary: t('studio.cards.previewEmptyDescription'),
    personality: [],
    interests: [],
    speaking_style: '',
    boundaries: [],
    aspirations: [],
    appearance: '',
    gender_identity: '',
    third_person_pronoun: '',
    visual_gender_presentation: '',
    visual_subject_type: 'auto',
    date_of_birth: null,
    disposition: {
      self_centeredness: 'medium',
      candor: 'medium',
      sharing_drive: 'medium',
      associativeness: 'medium',
    },
    personality_type: {
      system: 'mbti_16',
      code: '',
      source: 'unset',
      confidence: 0,
      rationale: '',
      consistency_notes: [],
    },
    world_frame: '',
    world_awareness_enabled: false,
    world_topics: [],
    subscribed_categories: [],
    excluded_topics: [],
    proactive_enabled: false,
    proactive_daily_limit: 0,
    proactive_cooldown_minutes: 0,
    accepts_web_proactive: false,
    feed_daily_limit: 0,
    companions: [],
    has_main_arc: false,
    bundled_arc_template_count: 0,
    bundled_arc_titles: [],
    has_arc_series: bundledSeries.length > 0,
    bundled_arc_series_count: bundledSeries.length,
    bundled_arc_series_titles: bundledSeries.map((item) => item.title),
    bundled_arc_series_member_count: totalSeriesMembers(bundledSeries),
    stage_image_count: 0,
    image_urls: [],
  }
}

function buildCharacterPreview(
  character: Character,
  bundledSeries: ArcSeries[],
): CharacterCardPreview {
  return {
    pack_id: null,
    title: character.name,
    author: 'Yuralume',
    description: character.summary,
    tags: character.interests,
    note: t('studio.cards.previewNote'),
    name: character.name,
    summary: character.summary,
    personality: character.personality,
    interests: character.interests,
    speaking_style: character.speaking_style,
    boundaries: character.boundaries,
    aspirations: character.aspirations,
    appearance: character.appearance,
    gender_identity: character.gender_identity,
    third_person_pronoun: character.third_person_pronoun,
    visual_gender_presentation: character.visual_gender_presentation,
    visual_subject_type: character.visual_subject_type,
    date_of_birth: character.date_of_birth,
    disposition: character.disposition,
    personality_type: character.personality_type,
    world_frame: character.world_frame,
    world_awareness_enabled: character.world_awareness_enabled,
    world_topics: character.world_topics,
    subscribed_categories: character.subscribed_categories,
    excluded_topics: character.excluded_topics,
    proactive_enabled: character.proactive_enabled,
    proactive_daily_limit: character.proactive_daily_limit,
    proactive_cooldown_minutes: character.proactive_cooldown_minutes,
    accepts_web_proactive: character.accepts_web_proactive,
    feed_daily_limit: character.feed_daily_limit,
    companions: character.companions.map((companion) => ({
      name: companion.name,
      role: companion.role,
    })),
    has_main_arc: Boolean(character.arc_template_id),
    bundled_arc_template_count: character.arc_template_id ? 1 : 0,
    bundled_arc_titles: [],
    has_arc_series: bundledSeries.length > 0,
    bundled_arc_series_count: bundledSeries.length,
    bundled_arc_series_titles: bundledSeries.map((item) => item.title),
    bundled_arc_series_member_count: totalSeriesMembers(bundledSeries),
    stage_image_count: character.image_urls.length,
    image_urls: character.image_urls,
  }
}

function totalSeriesMembers(items: ArcSeries[]): number {
  return items.reduce((sum, item) => sum + item.member_count, 0)
}
</script>

<template>
  <section class="studio-cards-page" aria-labelledby="studio-cards-title">
    <div class="studio-cards-page__copy">
      <h2 id="studio-cards-title">{{ t('studio.cards.title') }}</h2>
      <p>{{ t('studio.cards.subtitle') }}</p>
    </div>

    <div class="studio-cards-page__layout">
      <section class="studio-cards-page__panel glass-panel" aria-labelledby="card-export-title">
        <h3 id="card-export-title">{{ t('studio.cards.bundleTitle') }}</h3>

        <label class="field-label" for="card-character">
          {{ t('studio.cards.characterLabel') }}
        </label>
        <select
          id="card-character"
          v-model="selectedCharacterId"
          class="field-select"
        >
          <option value="">{{ t('studio.cards.characterPlaceholder') }}</option>
          <option
            v-for="character in characters"
            :key="character.id"
            :value="character.id"
          >
            {{ character.name }}
          </option>
        </select>

        <div class="studio-cards-page__series">
          <p class="field-label">{{ t('studio.cards.seriesLabel') }}</p>
          <label
            v-for="item in series"
            :key="item.id"
            class="studio-cards-page__series-row"
          >
            <input
              class="field-checkbox"
              type="checkbox"
              :checked="selectedSeriesIds.includes(item.id)"
              @change="handleSeriesChange(item.id, $event)"
            />
            <span>
              <strong>{{ item.title }}</strong>
              <small>
                {{ t('studio.cards.seriesMemberCount', { count: item.member_count }) }}
              </small>
            </span>
          </label>
          <p v-if="!series.length" class="field-hint">
            {{ t('studio.cards.noSeries') }}
          </p>
        </div>

        <p v-if="selectedCharacter" class="field-hint">
          {{ t('studio.cards.bundleHint') }}
        </p>
        <p v-if="error" class="studio-cards-page__error">{{ error }}</p>

        <UiButton
          variant="hero"
          :disabled="exportDisabled"
          @click="exportCard"
        >
          {{ t('studio.cards.exportButton') }}
        </UiButton>
      </section>

      <aside class="studio-cards-page__preview" aria-labelledby="card-preview-title">
        <p id="card-preview-title" class="spark-label">
          {{ t('studio.cards.previewTitle') }}
        </p>
        <CharacterCardFace :card="previewCard" />
      </aside>
    </div>
  </section>
</template>

<style scoped>
.studio-cards-page {
  display: grid;
  gap: var(--space-3);
}

.studio-cards-page__copy h2,
.studio-cards-page__copy p,
.studio-cards-page__panel h3,
.studio-cards-page__series p {
  margin: 0;
  letter-spacing: 0;
}

.studio-cards-page__copy h2 {
  font-size: 28px;
  font-family: var(--font-display);
  font-weight: 700;
}

.studio-cards-page__copy p {
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.studio-cards-page__layout {
  display: grid;
  grid-template-columns: minmax(280px, 520px) minmax(300px, 1fr);
  align-items: start;
  gap: var(--space-5);
}

.studio-cards-page__panel {
  padding: var(--space-4);
  border-radius: 8px;
  display: grid;
  gap: var(--space-3);
}

.studio-cards-page__panel h3 {
  font-size: var(--font-md);
  font-weight: 650;
}

.studio-cards-page__series {
  display: grid;
  gap: var(--space-2);
}

.studio-cards-page__series-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-height: 44px;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
}

.studio-cards-page__series-row span {
  min-width: 0;
  display: grid;
  gap: 2px;
}

.studio-cards-page__series-row strong,
.studio-cards-page__series-row small {
  overflow-wrap: anywhere;
  letter-spacing: 0;
}

.studio-cards-page__series-row small {
  color: var(--color-text-secondary);
}

.studio-cards-page__error {
  margin: 0;
  color: var(--color-danger);
}

.studio-cards-page__preview {
  min-width: 0;
  display: grid;
  justify-items: center;
  gap: var(--space-3);
}

.studio-cards-page__preview .spark-label {
  width: min(100%, 360px);
  margin: 0;
}

@media (max-width: 900px) {
  .studio-cards-page__layout {
    grid-template-columns: 1fr;
  }
}
</style>
