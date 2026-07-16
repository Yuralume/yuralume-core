<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  UiButton,
  UiInput,
  UiTextarea,
  UiSelect,
  UiCard,
  UiSection,
  UiBadge,
} from '@/components/ui'
import CharacterRelationshipMood from '@/components/CharacterRelationshipMood.vue'
import CharacterCardFace from '@/components/CharacterCardFace.vue'
import CharacterCardGalleryModal from '@/components/CharacterCardGalleryModal.vue'
import type { CharacterCardPreview } from '@/utils/api/characters'

const { t } = useI18n()

const text = ref('')
const num = ref<number | string>(0)
const note = ref('')
const choice = ref('a')

const options = computed(() => [
  { value: 'a', label: t('styleGuide.options.a') },
  { value: 'b', label: t('styleGuide.options.b') },
  { value: 'c', label: t('styleGuide.options.c'), disabled: true },
])

const loadingDemo = ref(false)
function toggleLoading() {
  loadingDemo.value = true
  setTimeout(() => { loadingDemo.value = false }, 1500)
}

const sampleCharacterCard: CharacterCardPreview = {
  pack_id: 'styleguide',
  title: 'Mio Cafe Idol',
  author: 'Yuralume',
  description: 'A warm cafe singer with a small stage and a bigger dream.',
  tags: ['daily life', 'music', 'cafe'],
  note: 'Best with a soft illustrated image profile.',
  name: 'Mio',
  summary: 'A college student who sings in a neighborhood cafe.',
  personality: ['warm', 'curious'],
  interests: ['coffee', 'singing'],
  speaking_style: 'gentle and lively',
  boundaries: ['keeps public conflict at a distance'],
  aspirations: ['perform on a real stage'],
  appearance: 'Apron, shoulder-length hair, and a small ribbon.',
  gender_identity: 'woman',
  third_person_pronoun: 'she',
  visual_gender_presentation: 'feminine cafe singer',
  visual_subject_type: 'human',
  date_of_birth: '2003-04-12',
  disposition: {
    self_centeredness: 'medium',
    candor: 'medium',
    sharing_drive: 'high',
    associativeness: 'medium',
  },
  personality_type: {
    system: 'mbti_16',
    code: 'ENFP',
    source: 'user_explicit',
    confidence: 1,
    rationale: 'Expressive, people-focused, and driven by new possibilities.',
    consistency_notes: ['Keep concrete cafe routines more important than the type label.'],
  },
  world_frame: 'modern',
  world_awareness_enabled: true,
  world_topics: ['local music', 'coffee shops'],
  subscribed_categories: ['culture'],
  excluded_topics: ['scandals'],
  proactive_enabled: true,
  proactive_daily_limit: 3,
  proactive_cooldown_minutes: 45,
  accepts_web_proactive: true,
  feed_daily_limit: 2,
  companions: [{ name: 'Manager', role: 'cafe owner' }],
  has_main_arc: true,
  bundled_arc_template_count: 1,
  bundled_arc_titles: ['Cafe audition week'],
  has_arc_series: true,
  bundled_arc_series_count: 1,
  bundled_arc_series_titles: ['Cafe idol season'],
  bundled_arc_series_member_count: 2,
  stage_image_count: 0,
  image_urls: [],
}

const galleryOpen = ref(false)
const galleryIndex = ref(0)
const galleryCards: CharacterCardPreview[] = [
  sampleCharacterCard,
  {
    ...sampleCharacterCard,
    pack_id: 'styleguide-2',
    name: 'Rin',
    title: 'Night Market Runner',
    summary: 'A quick-footed courier who knows every alley after dark.',
    tags: ['city', 'mystery'],
  },
  {
    ...sampleCharacterCard,
    pack_id: 'styleguide-3',
    name: 'Aoi',
    title: 'Seaside Painter',
    summary: 'A painter chasing the exact blue of the morning sea.',
    tags: ['art', 'calm'],
  },
]
</script>

<template>
  <div class="style-guide">
    <header class="style-guide__header">
      <h1>{{ t('styleGuide.title') }}</h1>
      <p class="hint">
        {{ t('styleGuide.hintPrefix') }} <code>/_styleguide</code>{{ t('styleGuide.hintSuffix') }}
      </p>
    </header>

    <UiSection title="UiButton — variant" description="primary / secondary / danger / ghost / chip">
      <div class="row">
        <UiButton variant="primary">Primary</UiButton>
        <UiButton variant="secondary">Secondary</UiButton>
        <UiButton variant="danger">Danger</UiButton>
        <UiButton variant="ghost">Ghost</UiButton>
        <UiButton variant="chip">Chip</UiButton>
        <UiButton variant="hero">Hero</UiButton>
        <UiButton variant="chip" active>Chip Active</UiButton>
      </div>
    </UiSection>

    <UiSection
      :title="t('styleGuide.brandEffects.title')"
      :description="t('styleGuide.brandEffects.description')"
    >
      <div class="brand-effects-demo glass-panel sheen-hover">
        <p class="spark-label">{{ t('styleGuide.brandEffects.spark') }}</p>
        <h2 class="display-title display-title--gradient">
          {{ t('styleGuide.brandEffects.displayTitle') }}
        </h2>
        <p>{{ t('styleGuide.brandEffects.body') }}</p>
        <UiButton variant="hero">{{ t('styleGuide.brandEffects.cta') }}</UiButton>
      </div>
    </UiSection>

    <UiSection title="UiButton — size" description="sm / md / lg">
      <div class="row">
        <UiButton variant="primary" size="sm">Small</UiButton>
        <UiButton variant="primary" size="md">Medium</UiButton>
        <UiButton variant="primary" size="lg">Large</UiButton>
      </div>
    </UiSection>

    <UiSection title="UiButton — state" description="disabled / loading / block">
      <div class="row">
        <UiButton variant="primary" disabled>Disabled</UiButton>
        <UiButton variant="primary" :loading="loadingDemo" @click="toggleLoading">
          {{ loadingDemo ? t('styleGuide.loading.busy') : t('styleGuide.loading.action') }}
        </UiButton>
      </div>
      <UiButton variant="secondary" block>Block button (width: 100%)</UiButton>
    </UiSection>

    <UiSection title="UiInput" description="text / number / password / date">
      <div class="grid">
        <UiInput v-model="text" :label="t('styleGuide.inputs.name')" :placeholder="t('styleGuide.inputs.namePlaceholder')" :hint="t('styleGuide.inputs.hint')" />
        <UiInput v-model="num" type="number" :label="t('styleGuide.inputs.age')" :min="0" :max="120" required />
        <UiInput type="password" :label="t('styleGuide.inputs.password')" placeholder="••••••" />
        <UiInput type="date" :label="t('styleGuide.inputs.date')" />
        <UiInput :label="t('styleGuide.inputs.disabled')" :placeholder="t('styleGuide.inputs.disabledPlaceholder')" disabled />
        <UiInput :label="t('styleGuide.inputs.readonly')" :model-value="t('styleGuide.inputs.readonlyValue')" readonly />
      </div>
    </UiSection>

    <UiSection title="UiTextarea">
      <UiTextarea v-model="note" :label="t('styleGuide.textarea.label')" :placeholder="t('styleGuide.textarea.placeholder')" :rows="4" :hint="t('styleGuide.textarea.hint')" :maxlength="200" />
    </UiSection>

    <UiSection title="UiSelect">
      <div class="grid">
        <UiSelect v-model="choice" :options="options" :label="t('styleGuide.select.label')" :hint="t('styleGuide.select.hint')" />
        <UiSelect v-model="choice" :options="options" :label="t('styleGuide.select.disabled')" disabled />
      </div>
    </UiSection>

    <UiSection title="UiCard">
      <UiCard :title="t('styleGuide.card.title')">
        <p>{{ t('styleGuide.card.body') }}</p>
        <template #actions>
          <UiButton variant="ghost" size="sm">{{ t('common.actions.edit') }}</UiButton>
        </template>
        <template #footer>
          <div class="row">
            <UiButton variant="primary" size="sm">{{ t('common.actions.confirm') }}</UiButton>
            <UiButton variant="secondary" size="sm">{{ t('common.actions.cancel') }}</UiButton>
          </div>
        </template>
      </UiCard>

      <UiCard size="lg" hoverable :title="t('styleGuide.card.hoverTitle')">
        <p>{{ t('styleGuide.card.hoverBody') }}</p>
      </UiCard>
    </UiSection>

    <UiSection title="UiBadge">
      <div class="row">
        <UiBadge>Default</UiBadge>
        <UiBadge variant="primary">Primary</UiBadge>
        <UiBadge variant="success">Success</UiBadge>
        <UiBadge variant="warning">Warning</UiBadge>
        <UiBadge variant="danger">Danger</UiBadge>
      </div>
    </UiSection>

    <UiSection title="CharacterRelationshipMood" :description="t('styleGuide.relationshipMood.description')">
      <div class="row">
        <CharacterRelationshipMood
          :emotion="t('styleGuide.relationshipMood.sampleEmotion')"
          :affection="73"
          :energy="45"
        />
        <CharacterRelationshipMood
          :emotion="t('styleGuide.relationshipMood.headerEmotion')"
          variant="header"
          :show-metrics="false"
        />
      </div>
    </UiSection>

    <UiSection title="CharacterCardFace">
      <div class="card-face-sample">
        <CharacterCardFace
          :card="sampleCharacterCard"
          :action-label="t('playerSidebar.characterCards.installAction')"
        />
      </div>
    </UiSection>

    <UiSection title="CharacterCardGalleryModal — browse" description="side peeks + slide animation on navigate">
      <UiButton variant="primary" @click="galleryOpen = true">Open gallery</UiButton>
      <CharacterCardGalleryModal
        :visible="galleryOpen"
        mode="browse"
        :cards="galleryCards"
        :active-index="galleryIndex"
        @close="galleryOpen = false"
        @change="galleryIndex = $event"
      />
    </UiSection>

    <UiSection title="Design tokens" :description="t('styleGuide.tokens.description')" :bordered="false">
      <UiCard>
        <ul class="tokens">
          <li><code>--space-1</code> ~ <code>--space-6</code>: 4 / 8 / 12 / 16 / 24 / 32</li>
          <li><code>--btn-radius</code>: 6px; <code>--card-radius</code>: 8px</li>
          <li><code>--font-xs</code> ~ <code>--font-xl</code>: 11 / 12 / 13 / 15 / 18</li>
          <li><code>--color-primary</code>, <code>--color-surface</code>, <code>--color-border</code>... ({{ t('styleGuide.tokens.seeStyle') }} <code>style.css</code>)</li>
        </ul>
      </UiCard>
    </UiSection>
  </div>
</template>

<style scoped>
.style-guide {
  max-width: 920px;
  margin: 0 auto;
  padding: var(--space-5) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  height: 100%;
  overflow-y: auto;
}
.style-guide__header h1 {
  font-size: var(--font-xl);
  margin: 0 0 var(--space-1);
}
.style-guide__header .hint {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  align-items: center;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--space-3);
}
.brand-effects-demo {
  width: min(100%, 560px);
  padding: var(--space-5);
  border-radius: 8px;
  display: grid;
  gap: var(--space-3);
}
.brand-effects-demo h2,
.brand-effects-demo p {
  margin: 0;
}
.brand-effects-demo h2 {
  font-size: 34px;
}
.brand-effects-demo p:not(.spark-label) {
  max-width: 46ch;
  color: var(--color-text-secondary);
  line-height: 1.7;
}
.card-face-sample {
  display: flex;
  justify-content: center;
}
.tokens {
  margin: 0;
  padding-left: var(--space-4);
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.7;
}
.tokens code {
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: var(--font-xs);
  color: var(--color-text);
}
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
</style>
