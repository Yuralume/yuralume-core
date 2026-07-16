<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiBadge, UiButton } from '@/components/ui'
import type { CharacterCardPreview } from '@/utils/api/characters'

const props = withDefaults(
  defineProps<{
    card: CharacterCardPreview
    actionLabel?: string
    actionLoading?: boolean
    actionDisabled?: boolean
  }>(),
  {
    actionLabel: '',
    actionLoading: false,
    actionDisabled: false,
  },
)

const emit = defineEmits<{
  action: []
}>()

const { t } = useI18n()

const detailsOpen = ref(false)
const activeImageIndex = ref(0)
const failedImageIndexes = ref<Set<number>>(new Set())

const title = computed(() => props.card.name || props.card.title)
const intro = computed(() => props.card.summary || props.card.description)
const activeImage = computed(() => props.card.image_urls[activeImageIndex.value] ?? '')
const showImage = computed(() => activeImage.value && !failedImageIndexes.value.has(activeImageIndex.value))
const initial = computed(() => (props.card.name || title.value || '?').trim().charAt(0).toUpperCase())

const detailRows = computed(() => {
  const rows: Array<{ label: string; value: string }> = []
  addRow(rows, 'summary', props.card.summary)
  addListRow(rows, 'personality', props.card.personality)
  addListRow(rows, 'interests', props.card.interests)
  addRow(rows, 'speakingStyle', props.card.speaking_style)
  addListRow(rows, 'boundaries', props.card.boundaries)
  addListRow(rows, 'aspirations', props.card.aspirations)
  addRow(rows, 'appearance', props.card.appearance)
  addRow(rows, 'genderIdentity', props.card.gender_identity)
  addRow(rows, 'pronoun', props.card.third_person_pronoun)
  addRow(rows, 'visualPresentation', props.card.visual_gender_presentation)
  addRow(
    rows,
    'visualSubjectType',
    props.card.visual_subject_type
      ? t(`characterCreate.fields.visualSubjectType.options.${props.card.visual_subject_type}`)
      : '',
  )
  addRow(rows, 'birthday', props.card.date_of_birth ?? '')
  addRow(rows, 'worldFrame', props.card.world_frame)
  addListRow(rows, 'worldTopics', props.card.world_topics)
  addListRow(rows, 'subscribedCategories', props.card.subscribed_categories)
  addListRow(rows, 'excludedTopics', props.card.excluded_topics)
  addRow(rows, 'disposition', dispositionLabel.value)
  addRow(rows, 'personalityType', personalityTypeLabel.value)
  addRow(rows, 'cadence', cadenceLabel.value)
  addListRow(rows, 'companions', props.card.companions.map((c) => c.role ? `${c.name} (${c.role})` : c.name))
  addListRow(rows, 'arcTitles', props.card.bundled_arc_titles)
  addListRow(rows, 'arcSeriesTitles', props.card.bundled_arc_series_titles)
  if (props.card.bundled_arc_series_member_count) {
    addRow(
      rows,
      'arcSeriesMembers',
      String(props.card.bundled_arc_series_member_count),
    )
  }
  addRow(rows, 'note', props.card.note)
  return rows
})

const dispositionLabel = computed(() => {
  const entries = [
    ['self_centeredness', props.card.disposition.self_centeredness],
    ['candor', props.card.disposition.candor],
    ['sharing_drive', props.card.disposition.sharing_drive],
    ['associativeness', props.card.disposition.associativeness],
  ] as const
  return entries.map(([dimension, band]) => (
    `${t(`playerSidebar.characterCards.details.dispositionDimensions.${dimension}`)}: ${t(`playerSidebar.characterCards.details.dispositionBands.${band}`)}`
  )).join(' / ')
})

const personalityTypeLabel = computed(() => {
  const type = props.card.personality_type
  if (!type?.code) return ''
  return type.rationale ? `${type.code} - ${type.rationale}` : type.code
})

const cadenceLabel = computed(() => props.card.proactive_enabled
  ? t('playerSidebar.characterCards.details.cadenceEnabled', {
    daily: props.card.proactive_daily_limit,
    cooldown: props.card.proactive_cooldown_minutes,
  })
  : t('playerSidebar.characterCards.details.cadenceDisabled'))

watch(() => props.card, () => {
  detailsOpen.value = false
  activeImageIndex.value = 0
  failedImageIndexes.value = new Set()
})

function addRow(
  rows: Array<{ label: string; value: string }>,
  key: string,
  value: string,
) {
  const cleaned = value.trim()
  if (!cleaned) return
  rows.push({
    label: t(`playerSidebar.characterCards.details.fields.${key}`),
    value: cleaned,
  })
}

function addListRow(
  rows: Array<{ label: string; value: string }>,
  key: string,
  values: string[],
) {
  const joined = values.map((value) => value.trim()).filter(Boolean).join(t('common.listSeparator'))
  if (!joined) return
  rows.push({
    label: t(`playerSidebar.characterCards.details.fields.${key}`),
    value: joined,
  })
}

function selectImage(index: number) {
  activeImageIndex.value = index
}

function markImageFailed() {
  const next = new Set(failedImageIndexes.value)
  next.add(activeImageIndex.value)
  failedImageIndexes.value = next
}
</script>

<template>
  <article class="character-card-face">
    <div class="character-card-face__inner">
      <header class="character-card-face__nameplate">
        <h3 class="character-card-face__title">{{ title }}</h3>
        <span v-if="card.author" class="character-card-face__author">
          {{ card.author }}
        </span>
      </header>

      <div class="character-card-face__art">
        <img
          v-if="showImage"
          class="character-card-face__image"
          :src="activeImage"
          :alt="title"
          @error="markImageFailed"
        />
        <div v-else class="character-card-face__fallback" aria-hidden="true">
          {{ initial }}
        </div>

        <span class="character-card-face__sheen" aria-hidden="true" />

        <div v-if="card.image_urls.length > 1" class="character-card-face__dots">
          <button
            v-for="(_url, index) in card.image_urls"
            :key="`${card.name}-${index}`"
            type="button"
            class="character-card-face__dot"
            :class="{ 'is-active': index === activeImageIndex }"
            :aria-label="t('playerSidebar.characterCards.gallery.imagePage', {
              current: index + 1,
              total: card.image_urls.length,
            })"
            @click="selectImage(index)"
          />
        </div>
      </div>

      <div class="character-card-face__body">
        <div v-if="card.tags.length" class="character-card-face__tags">
          <UiBadge v-for="tag in card.tags" :key="tag" variant="default">
            {{ tag }}
          </UiBadge>
        </div>

        <p v-if="intro" class="character-card-face__intro">{{ intro }}</p>

        <div
          v-if="card.has_main_arc || card.bundled_arc_template_count || card.has_arc_series || card.stage_image_count"
          class="character-card-face__badges"
        >
          <UiBadge v-if="card.has_main_arc || card.bundled_arc_template_count" variant="warning">
            {{ t('playerSidebar.characterCards.storySeedCount', {
              count: card.bundled_arc_template_count,
            }) }}
          </UiBadge>
          <UiBadge v-if="card.has_arc_series || card.bundled_arc_series_count" variant="success">
            {{ t('playerSidebar.characterCards.arcSeriesCount', {
              count: card.bundled_arc_series_count,
            }) }}
          </UiBadge>
          <UiBadge v-if="card.stage_image_count" variant="primary">
            {{ t('playerSidebar.characterCards.stageImageCount', {
              count: card.stage_image_count,
            }) }}
          </UiBadge>
        </div>

        <button
          v-if="detailRows.length"
          type="button"
          class="character-card-face__details-toggle"
          :aria-expanded="detailsOpen"
          @click="detailsOpen = !detailsOpen"
        >
          {{ detailsOpen
            ? t('playerSidebar.characterCards.details.hide')
            : t('playerSidebar.characterCards.details.show') }}
        </button>

        <dl v-if="detailsOpen" class="character-card-face__details">
          <div
            v-for="row in detailRows"
            :key="row.label"
            class="character-card-face__detail-row"
          >
            <dt>{{ row.label }}</dt>
            <dd>{{ row.value }}</dd>
          </div>
        </dl>

        <UiButton
          v-if="actionLabel"
          variant="primary"
          block
          :loading="actionLoading"
          :disabled="actionDisabled"
          @click="emit('action')"
        >
          {{ actionLabel }}
        </UiButton>
      </div>
    </div>
  </article>
</template>

<style scoped>
/* 收藏卡質感：金屬 foil 描邊（padding 留出邊框）包住內層暗色卡身。 */
.character-card-face {
  position: relative;
  width: min(320px, 100%);
  padding: 7px;
  border-radius: 18px;
  background:
    linear-gradient(
      150deg,
      rgba(255, 209, 128, 0.85),
      rgba(201, 143, 219, 0.6) 30%,
      rgba(139, 109, 255, 0.5) 52%,
      rgba(255, 209, 128, 0.42) 100%
    );
  box-shadow:
    0 18px 54px rgba(0, 0, 0, 0.46),
    0 2px 0 rgba(255, 255, 255, 0.14) inset;
  transition: transform 0.28s ease, box-shadow 0.28s ease;
}

.character-card-face:hover {
  transform: translateY(-3px);
  box-shadow:
    0 26px 68px rgba(0, 0, 0, 0.54),
    0 0 0 1px rgba(255, 209, 128, 0.5),
    0 2px 0 rgba(255, 255, 255, 0.18) inset;
}

.character-card-face__inner {
  display: flex;
  flex-direction: column;
  border-radius: 12px;
  background:
    radial-gradient(120% 60% at 50% -8%, rgba(255, 209, 128, 0.16), transparent 60%),
    linear-gradient(180deg, rgba(33, 24, 64, 0.98), rgba(17, 13, 36, 0.99));
  box-shadow: 0 0 0 1px rgba(0, 0, 0, 0.4) inset;
  overflow: hidden;
}

.character-card-face__nameplate {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
  padding: 10px 14px 8px;
  border-bottom: 1px solid rgba(255, 209, 128, 0.22);
  background: linear-gradient(180deg, rgba(255, 209, 128, 0.1), transparent);
}

.character-card-face__title {
  min-width: 0;
  margin: 0;
  color: var(--color-text);
  font-family: var(--font-display);
  font-size: 23px;
  letter-spacing: 0.4px;
  line-height: 1.2;
  font-weight: 600;
  overflow-wrap: anywhere;
  text-shadow: 0 1px 8px rgba(255, 209, 128, 0.18);
}

.character-card-face__author {
  flex-shrink: 0;
  color: var(--color-spark);
  font-size: var(--font-xs);
  letter-spacing: 0.3px;
  opacity: 0.85;
}

/* 內嵌藝術窗：內框 + 內陰影，框出主圖。 */
.character-card-face__art {
  position: relative;
  margin: 10px 12px 0;
  aspect-ratio: 3 / 4;
  border-radius: 9px;
  overflow: hidden;
  background: rgba(255, 255, 255, 0.04);
  box-shadow:
    0 0 0 1px rgba(255, 255, 255, 0.16) inset,
    0 8px 22px rgba(0, 0, 0, 0.42);
}

.character-card-face__image {
  width: 100%;
  height: 100%;
  display: block;
  object-fit: cover;
}

.character-card-face__fallback {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
  background:
    radial-gradient(circle at 36% 24%, rgba(255, 209, 128, 0.4), transparent 36%),
    linear-gradient(135deg, rgba(95, 213, 164, 0.22), rgba(139, 109, 255, 0.28)),
    rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-family: var(--font-display);
  font-size: 74px;
  line-height: 1;
  text-shadow: 0 2px 14px rgba(0, 0, 0, 0.4);
}

/* 全像光澤：斜向高光帶，懸停時掃過卡面。 */
.character-card-face__sheen {
  position: absolute;
  inset: 0;
  pointer-events: none;
  background: linear-gradient(
    122deg,
    transparent 32%,
    rgba(255, 255, 255, 0.22) 46%,
    rgba(201, 143, 219, 0.18) 52%,
    transparent 66%
  );
  background-size: 250% 250%;
  background-position: 0% 0%;
  mix-blend-mode: screen;
  opacity: 0.65;
  transition: background-position 0.9s ease;
}

.character-card-face:hover .character-card-face__sheen {
  background-position: 100% 100%;
}

.character-card-face__dots {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 10px;
  display: flex;
  justify-content: center;
  gap: 6px;
}

.character-card-face__dot {
  width: 8px;
  height: 8px;
  border: 1px solid rgba(255, 255, 255, 0.8);
  border-radius: 50%;
  background: rgba(0, 0, 0, 0.42);
  padding: 0;
  cursor: pointer;
  transition: transform 0.15s ease;
}

.character-card-face__dot:hover {
  transform: scale(1.2);
}

.character-card-face__dot.is-active {
  background: var(--color-spark);
  border-color: var(--color-spark);
  box-shadow: 0 0 8px rgba(255, 209, 128, 0.7);
}

.character-card-face__body {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
}

.character-card-face__tags,
.character-card-face__badges {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-1);
}

/* 描述框：內嵌面板，仿卡牌底部說明欄。完整顯示，過長交給 modal 捲動。 */
.character-card-face__intro {
  margin: 0;
  padding: 8px 10px;
  border-radius: 7px;
  border: 1px solid rgba(255, 255, 255, 0.07);
  background: rgba(255, 255, 255, 0.035);
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  line-height: 1.6;
  white-space: pre-line;
  overflow-wrap: anywhere;
}

.character-card-face__details-toggle {
  align-self: flex-start;
  border: 0;
  background: transparent;
  color: var(--color-primary-light);
  cursor: pointer;
  font: inherit;
  font-size: var(--font-xs);
  letter-spacing: 0.2px;
  padding: 2px 0;
}

.character-card-face__details-toggle:hover {
  color: var(--color-spark);
  text-decoration: underline;
}

.character-card-face__details {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: 220px;
  overflow-y: auto;
  padding: var(--space-2);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.035);
}

.character-card-face__detail-row {
  display: grid;
  gap: 2px;
}

.character-card-face__detail-row dt {
  color: var(--color-spark);
  font-size: var(--font-xs);
  font-weight: 650;
  letter-spacing: 0.2px;
}

.character-card-face__detail-row dd {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

@media (prefers-reduced-motion: reduce) {
  .character-card-face,
  .character-card-face__sheen,
  .character-card-face__dot {
    transition: none;
  }

  .character-card-face:hover {
    transform: none;
  }
}
</style>
