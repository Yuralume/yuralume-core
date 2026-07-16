<script setup lang="ts">
/**
 * LLM routing cost what-if simulator (Observability → Cost modeling tab).
 *
 * Loads the *live* routing config (feature groups + per-feature overrides +
 * global active model) as a baseline, lets the operator re-assign models per
 * group / per feature, and re-prices the recorded 30-day token volume under
 * the edited scenario — so the bill impact of a price change, a new model, or
 * a routing tweak is visible without touching the DB.
 *
 * All math is in the pure ``utils/costModeling.ts`` module; prices are the
 * shared ``usagePricing`` pool (entered once, reused by the usage calculator).
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  type UsageCharacterBucket,
  type UsageCharacterFeatureBucket,
  usageByCharacter,
  usageByCharacterFeature,
} from '@/utils/api/observability'
import {
  type FeatureModelGroupSummary,
  getActiveModelPreference,
  getFeatureModelGroups,
  getFeatureModelPreferences,
} from '@/utils/api/system'
import {
  type ModelRef,
  type Scenario,
  type ScenarioBucket,
  type ScenarioRouting,
  buildFeatureGroupIndex,
  computeCharacterNoise,
  computeScenarioCost,
  deleteScenario,
  loadScenarios,
  saveScenario,
} from '@/utils/costModeling'
import {
  type PriceBook,
  type PriceEntry,
  loadPriceBook,
  priceBookKey,
  savePriceBook,
} from '@/utils/usagePricing'
import { useLocale } from '@/composables/useLocale'
import { UiButton } from '@/components/ui'

const props = withDefaults(
  defineProps<{ characters?: { id: string; name: string }[] }>(),
  { characters: () => [] },
)

const { t } = useI18n()
const { locale } = useLocale()

const storage: Storage | null =
  typeof window !== 'undefined' ? window.localStorage : null

// --- date window (mirrors the usage tab's 30-day default) -------------------
function dayOffset(days: number): string {
  const day = new Date()
  day.setUTCDate(day.getUTCDate() + days)
  return day.toISOString().slice(0, 10)
}
const fromDate = ref(dayOffset(-30))
const toDate = ref(dayOffset(0))
const windowDays = computed(() => {
  const from = Date.parse(`${fromDate.value}T00:00:00Z`)
  const to = Date.parse(`${toDate.value}T23:59:59Z`)
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return 30
  return Math.max(1, Math.round((to - from) / 86_400_000))
})

// --- loaded state -----------------------------------------------------------
const loading = ref(false)
const error = ref<string | null>(null)
const configLoaded = ref(false)
const groups = ref<FeatureModelGroupSummary[]>([])
const featureToGroup = ref<Record<string, string>>({})
const knownFeatureKeys = ref<string[]>([])
const featureLabels = ref<Record<string, string>>({})
const cfBuckets = ref<UsageCharacterFeatureBucket[]>([])
const characterRows = ref<UsageCharacterBucket[]>([])

// --- editable scenario ------------------------------------------------------
const groupModels = reactive<Record<string, ModelRef>>({})
const featureOverrides = reactive<Record<string, ModelRef>>({})
const fallback = reactive<ModelRef>({ providerId: '', modelId: '' })
const baselineRouting = ref<ScenarioRouting | null>(null)

// shared price pool
const prices = reactive<PriceBook>(loadPriceBook(storage))

// named scenario library
const scenarioLibrary = ref(loadScenarios(storage))
const newScenarioName = ref('')

// override-add controls
const overrideFeatureDraft = ref('')

const currencyFmt = computed(() =>
  new Intl.NumberFormat(locale.value, {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 4,
  }),
)
function formatCost(amount: number): string {
  return currencyFmt.value.format(Number.isFinite(amount) ? amount : 0)
}
function formatSignedCost(amount: number): string {
  // amount = baseline − scenario, so a positive amount means the scenario is
  // cheaper (a saving). Keep the sign mathematically consistent with that —
  // "+" for savings, "−" for a cost increase — and pair it with an explicit
  // saved/increased tag in the template so the meaning never depends on the
  // sign alone (see deltaSavedTag / deltaIncreasedTag).
  const sign = amount > 0 ? '+' : amount < 0 ? '−' : ''
  return `${sign}${formatCost(Math.abs(amount))}`
}
function characterName(id: string | null): string {
  if (!id) return t('observabilityPanel.usage.byCharacter.unattributed')
  return props.characters.find((c) => c.id === id)?.name ?? id
}

// ---------------------------------------------------------------------------
async function loadUsage(): Promise<void> {
  const params = {
    from: `${fromDate.value}T00:00:00Z`,
    to: `${toDate.value}T23:59:59Z`,
    capability: 'llm' as const,
  }
  const [cf, chars] = await Promise.all([
    usageByCharacterFeature(params),
    usageByCharacter(params),
  ])
  cfBuckets.value = cf.filter((row) => row.capability === 'llm')
  characterRows.value = chars
}

async function loadConfig(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const [groupsPref, featurePref, active] = await Promise.all([
      getFeatureModelGroups('global'),
      getFeatureModelPreferences('global'),
      getActiveModelPreference('global'),
    ])
    groups.value = groupsPref.groups
    featureToGroup.value = buildFeatureGroupIndex(groupsPref.groups)
    knownFeatureKeys.value = featurePref.known_keys
    featureLabels.value = featurePref.labels

    // Seed the editable maps from the live config.
    for (const key of Object.keys(groupModels)) delete groupModels[key]
    for (const group of groupsPref.groups) {
      groupModels[group.key] = {
        providerId: group.model?.provider_id ?? '',
        modelId: group.model?.model_id ?? '',
      }
    }
    for (const key of Object.keys(featureOverrides)) delete featureOverrides[key]
    for (const [key, entry] of Object.entries(featurePref.overrides)) {
      if (entry.provider_id || entry.model_id) {
        featureOverrides[key] = {
          providerId: entry.provider_id ?? '',
          modelId: entry.model_id ?? '',
        }
      }
    }
    fallback.providerId = active.provider_id ?? ''
    fallback.modelId = active.model_id ?? ''

    baselineRouting.value = snapshotRouting()

    await loadUsage()
    configLoaded.value = true
  } catch (err) {
    error.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

async function reloadUsage(): Promise<void> {
  if (!configLoaded.value) return
  loading.value = true
  error.value = null
  try {
    await loadUsage()
  } catch (err) {
    error.value = err instanceof Error ? err.message : t('observabilityPanel.errors.loadFailed')
  } finally {
    loading.value = false
  }
}

// --- scenario snapshots -----------------------------------------------------
function realOnly(map: Record<string, ModelRef>): Record<string, ModelRef> {
  const out: Record<string, ModelRef> = {}
  for (const [key, model] of Object.entries(map)) {
    if (model.providerId || model.modelId) out[key] = { ...model }
  }
  return out
}
function snapshotRouting(): ScenarioRouting {
  return {
    groupAssignments: realOnly(groupModels),
    featureOverrides: realOnly(featureOverrides),
    fallback: fallback.providerId || fallback.modelId
      ? { providerId: fallback.providerId, modelId: fallback.modelId }
      : null,
  }
}

const scenario = computed<Scenario>(() => ({
  ...snapshotRouting(),
  priceBook: prices,
}))

const scenarioBuckets = computed<ScenarioBucket[]>(() =>
  cfBuckets.value.map((row) => ({
    characterId: row.character_id,
    featureKey: row.feature_key,
    capability: row.capability,
    inputTokens: row.total_input_quantity,
    outputTokens: row.total_output_quantity,
  })),
)

const scenarioResult = computed(() =>
  computeScenarioCost(scenarioBuckets.value, scenario.value, featureToGroup.value),
)
const baselineResult = computed(() => {
  if (!baselineRouting.value) return null
  return computeScenarioCost(
    scenarioBuckets.value,
    { ...baselineRouting.value, priceBook: prices },
    featureToGroup.value,
  )
})

const delta = computed(() => {
  if (!baselineResult.value) return 0
  return baselineResult.value.total - scenarioResult.value.total
})

const foregroundShare = computed(() => {
  const total = scenarioResult.value.total
  return total > 0 ? scenarioResult.value.foreground / total : 0
})

// active days per character key
const activeDaysById = computed<Record<string, number>>(() => {
  const map: Record<string, number> = {}
  for (const row of characterRows.value) {
    map[row.character_id ?? '__unattributed__'] = row.active_days
  }
  return map
})
const characterNoise = computed(() =>
  computeCharacterNoise(scenarioResult.value.perCharacter, activeDaysById.value),
)

// distinct price rows = union of baseline + scenario resolved models
const priceRows = computed(() => {
  const seen = new Map<string, { capability: string; providerId: string; modelId: string }>()
  const collect = (models: { capability: string; providerId: string; modelId: string }[]) => {
    for (const m of models) {
      const key = priceBookKey(m.capability, m.providerId, m.modelId)
      if (!seen.has(key)) seen.set(key, m)
    }
  }
  collect(scenarioResult.value.models)
  if (baselineResult.value) collect(baselineResult.value.models)
  return [...seen.entries()].map(([key, m]) => ({ key, ...m }))
})

// Unpriced-model warning: a model missing a price contributes 0 cost, which
// silently understates whichever side (scenario and/or baseline) resolves to
// it. Both sides must be checked — a typical "swap a pricey model out" flow
// leaves the *baseline* unpriced (it references the old, now-unpriced model),
// which would otherwise inflate the delta's apparent savings with no warning.
function modelLabel(model: { providerId: string; modelId: string }): string {
  if (model.providerId && model.modelId) return `${model.providerId}/${model.modelId}`
  return model.providerId || model.modelId
}
const unpricedScenarioModels = computed(() => [
  ...new Set(scenarioResult.value.models.filter((m) => !m.priced).map(modelLabel)),
])
const unpricedBaselineModels = computed(() =>
  baselineResult.value
    ? [...new Set(baselineResult.value.models.filter((m) => !m.priced).map(modelLabel))]
    : [],
)

function priceEntryFor(key: string): PriceEntry {
  if (!prices[key]) prices[key] = { inputPerMillion: '', outputPerMillion: '' }
  return prices[key]
}
function onPriceInput(key: string, field: 'inputPerMillion' | 'outputPerMillion', event: Event): void {
  priceEntryFor(key)[field] = (event.target as HTMLInputElement).value
  savePriceBook(storage, prices)
}

// --- override management -----------------------------------------------------
const overrideRows = computed(() =>
  Object.entries(featureOverrides).map(([key, model]) => ({
    featureKey: key,
    label: featureLabels.value[key] ?? key,
    model,
  })),
)
const availableOverrideKeys = computed(() =>
  knownFeatureKeys.value.filter((key) => !(key in featureOverrides)),
)
function addOverride(): void {
  const key = overrideFeatureDraft.value
  if (!key || key in featureOverrides) return
  featureOverrides[key] = { providerId: '', modelId: '' }
  overrideFeatureDraft.value = ''
}
function removeOverride(key: string): void {
  delete featureOverrides[key]
}

function resetToBaseline(): void {
  if (baselineRouting.value) applyRouting(baselineRouting.value)
}

function applyRouting(routing: ScenarioRouting): void {
  for (const key of Object.keys(groupModels)) {
    groupModels[key] = { providerId: '', modelId: '' }
  }
  for (const [key, model] of Object.entries(routing.groupAssignments)) {
    groupModels[key] = { ...model }
  }
  for (const key of Object.keys(featureOverrides)) delete featureOverrides[key]
  for (const [key, model] of Object.entries(routing.featureOverrides)) {
    featureOverrides[key] = { ...model }
  }
  fallback.providerId = routing.fallback?.providerId ?? ''
  fallback.modelId = routing.fallback?.modelId ?? ''
}

// --- named scenario library --------------------------------------------------
function persistScenario(): void {
  const name = newScenarioName.value.trim()
  if (!name) return
  scenarioLibrary.value = saveScenario(storage, name, snapshotRouting())
  newScenarioName.value = ''
}
function applyStoredScenario(name: string): void {
  const routing = scenarioLibrary.value[name]
  if (routing) applyRouting(routing)
}
function removeStoredScenario(name: string): void {
  scenarioLibrary.value = deleteScenario(storage, name)
}
const scenarioNames = computed(() => Object.keys(scenarioLibrary.value))

watch([fromDate, toDate], () => {
  if (configLoaded.value) reloadUsage()
})

onMounted(() => {
  loadConfig()
})

defineExpose({ loadConfig })
</script>

<template>
  <section class="cost-modeling">
    <p class="sub-hint">{{ t('observabilityPanel.costModeling.hint') }}</p>

    <div class="cm-toolbar">
      <label class="field-label">{{ t('observabilityPanel.usage.from') }}</label>
      <input v-model="fromDate" type="date" class="field-input cm-date" />
      <label class="field-label">{{ t('observabilityPanel.usage.to') }}</label>
      <input v-model="toDate" type="date" class="field-input cm-date" />
      <UiButton size="sm" :loading="loading" @click="loadConfig">
        {{ t('observabilityPanel.costModeling.loadConfig') }}
      </UiButton>
      <UiButton
        size="sm"
        variant="secondary"
        :disabled="!configLoaded || loading"
        @click="reloadUsage"
      >{{ t('observabilityPanel.costModeling.reloadUsage') }}</UiButton>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <p v-if="!configLoaded && !loading" class="empty">
      {{ t('observabilityPanel.costModeling.notLoaded') }}
    </p>

    <template v-if="configLoaded">
      <!-- Headline: baseline vs scenario over the window -->
      <div class="cm-headline">
        <div class="cm-card">
          <span class="metric-label">{{ t('observabilityPanel.costModeling.baselineCost', { days: windowDays }) }}</span>
          <strong>{{ formatCost(baselineResult?.total ?? 0) }}</strong>
        </div>
        <div class="cm-card">
          <span class="metric-label">{{ t('observabilityPanel.costModeling.scenarioCost', { days: windowDays }) }}</span>
          <strong>{{ formatCost(scenarioResult.total) }}</strong>
        </div>
        <div class="cm-card" :class="delta > 0 ? 'is-save' : delta < 0 ? 'is-cost' : ''">
          <span class="metric-label">{{ t('observabilityPanel.costModeling.delta') }}</span>
          <strong>
            {{ formatSignedCost(delta) }}
            <span v-if="delta !== 0" class="cm-delta-tag">
              {{ delta > 0
                ? t('observabilityPanel.costModeling.deltaSavedTag')
                : t('observabilityPanel.costModeling.deltaIncreasedTag') }}
            </span>
          </strong>
          <small>{{ t('observabilityPanel.costModeling.deltaHint') }}</small>
        </div>
        <div class="cm-card">
          <span class="metric-label">{{ t('observabilityPanel.costModeling.foregroundShare') }}</span>
          <strong>{{ (foregroundShare * 100).toFixed(1) }}%</strong>
          <small>
            {{ t('observabilityPanel.costModeling.foreground') }} {{ formatCost(scenarioResult.foreground) }}
            / {{ t('observabilityPanel.costModeling.background') }} {{ formatCost(scenarioResult.background) }}
          </small>
        </div>
      </div>

      <div v-if="unpricedBaselineModels.length || unpricedScenarioModels.length" class="cm-warn">
        <p v-if="unpricedBaselineModels.length">
          {{ t('observabilityPanel.costModeling.unpricedWarningBaseline', { models: unpricedBaselineModels.join(', ') }) }}
        </p>
        <p v-if="unpricedScenarioModels.length">
          {{ t('observabilityPanel.costModeling.unpricedWarningScenario', { models: unpricedScenarioModels.join(', ') }) }}
        </p>
      </div>

      <!-- Group routing editor -->
      <section class="cm-section">
        <div class="cm-section-head">
          <h4>{{ t('observabilityPanel.costModeling.groupRouting') }}</h4>
          <UiButton size="sm" variant="ghost" @click="resetToBaseline">
            {{ t('observabilityPanel.costModeling.resetToBaseline') }}
          </UiButton>
        </div>
        <p class="cm-hint">{{ t('observabilityPanel.costModeling.groupRoutingHint') }}</p>
        <div class="cm-scroll">
          <table class="cm-table">
            <thead>
              <tr>
                <th>{{ t('observabilityPanel.costModeling.group') }}</th>
                <th>{{ t('observabilityPanel.usage.provider') }}</th>
                <th>{{ t('observabilityPanel.usage.model') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="group in groups" :key="group.key">
                <td>
                  <span class="cm-group-label">{{ group.label }}</span>
                  <span class="cm-group-key">{{ group.key }}</span>
                </td>
                <td>
                  <input
                    v-if="groupModels[group.key]"
                    v-model="groupModels[group.key].providerId"
                    class="field-input cm-model-input"
                    :placeholder="t('observabilityPanel.costModeling.providerPlaceholder')"
                  />
                </td>
                <td>
                  <input
                    v-if="groupModels[group.key]"
                    v-model="groupModels[group.key].modelId"
                    class="field-input cm-model-input"
                    :placeholder="t('observabilityPanel.costModeling.modelPlaceholder')"
                  />
                </td>
              </tr>
              <tr class="cm-fallback-row">
                <td>{{ t('observabilityPanel.costModeling.fallbackLabel') }}</td>
                <td>
                  <input
                    v-model="fallback.providerId"
                    class="field-input cm-model-input"
                    :placeholder="t('observabilityPanel.costModeling.providerPlaceholder')"
                  />
                </td>
                <td>
                  <input
                    v-model="fallback.modelId"
                    class="field-input cm-model-input"
                    :placeholder="t('observabilityPanel.costModeling.modelPlaceholder')"
                  />
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- Advanced: single-feature overrides -->
      <details class="cm-section cm-advanced">
        <summary>{{ t('observabilityPanel.costModeling.advancedTitle') }}</summary>
        <p class="cm-hint">{{ t('observabilityPanel.costModeling.advancedHint') }}</p>
        <div class="cm-override-add">
          <select v-model="overrideFeatureDraft" class="field-select">
            <option value="">{{ t('observabilityPanel.costModeling.overrideFeature') }}</option>
            <option v-for="key in availableOverrideKeys" :key="key" :value="key">
              {{ featureLabels[key] ?? key }} ({{ key }})
            </option>
          </select>
          <UiButton size="sm" :disabled="!overrideFeatureDraft" @click="addOverride">
            {{ t('observabilityPanel.costModeling.addOverride') }}
          </UiButton>
        </div>
        <p v-if="overrideRows.length === 0" class="cm-hint">
          {{ t('observabilityPanel.costModeling.noOverrides') }}
        </p>
        <div v-else class="cm-scroll">
          <table class="cm-table">
            <thead>
              <tr>
                <th>{{ t('observabilityPanel.usage.feature') }}</th>
                <th>{{ t('observabilityPanel.usage.provider') }}</th>
                <th>{{ t('observabilityPanel.usage.model') }}</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in overrideRows" :key="row.featureKey">
                <td>
                  <span class="cm-group-label">{{ row.label }}</span>
                  <span class="cm-group-key">{{ row.featureKey }}</span>
                </td>
                <td>
                  <input
                    v-model="row.model.providerId"
                    class="field-input cm-model-input"
                    :placeholder="t('observabilityPanel.costModeling.providerPlaceholder')"
                  />
                </td>
                <td>
                  <input
                    v-model="row.model.modelId"
                    class="field-input cm-model-input"
                    :placeholder="t('observabilityPanel.costModeling.modelPlaceholder')"
                  />
                </td>
                <td>
                  <button type="button" class="cm-remove" @click="removeOverride(row.featureKey)">×</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </details>

      <!-- Price table (shared pool) -->
      <section class="cm-section">
        <h4>{{ t('observabilityPanel.costModeling.priceTitle') }}</h4>
        <p class="cm-hint">{{ t('observabilityPanel.costModeling.priceHint') }}</p>
        <div class="cm-scroll">
          <table class="cm-table">
            <thead>
              <tr>
                <th>{{ t('observabilityPanel.usage.provider') }}</th>
                <th>{{ t('observabilityPanel.usage.model') }}</th>
                <th class="num">{{ t('observabilityPanel.usage.priceCalc.inputPrice') }}</th>
                <th class="num">{{ t('observabilityPanel.usage.priceCalc.outputPrice') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in priceRows" :key="row.key">
                <td>{{ row.providerId || '-' }}</td>
                <td>{{ row.modelId || '-' }}</td>
                <td class="num">
                  <input
                    class="field-input cm-price"
                    type="text"
                    inputmode="decimal"
                    :value="priceEntryFor(row.key).inputPerMillion"
                    placeholder="0"
                    :aria-label="t('observabilityPanel.usage.priceCalc.inputPrice')"
                    @input="onPriceInput(row.key, 'inputPerMillion', $event)"
                  />
                </td>
                <td class="num">
                  <input
                    class="field-input cm-price"
                    type="text"
                    inputmode="decimal"
                    :value="priceEntryFor(row.key).outputPerMillion"
                    placeholder="0"
                    :aria-label="t('observabilityPanel.usage.priceCalc.outputPrice')"
                    @input="onPriceInput(row.key, 'outputPerMillion', $event)"
                  />
                </td>
              </tr>
              <tr v-if="priceRows.length === 0">
                <td colspan="4" class="empty">{{ t('observabilityPanel.usage.noUsage') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="cm-note">{{ t('observabilityPanel.usage.priceCalc.unitNote') }}</p>
      </section>

      <!-- Results: by group -->
      <section class="cm-section">
        <h4>{{ t('observabilityPanel.costModeling.byGroupTitle') }}</h4>
        <div class="cm-scroll">
          <table class="cm-table">
            <thead>
              <tr>
                <th>{{ t('observabilityPanel.costModeling.group') }}</th>
                <th class="num">{{ t('observabilityPanel.costModeling.foreground') }}</th>
                <th class="num">{{ t('observabilityPanel.costModeling.background') }}</th>
                <th class="num">{{ t('observabilityPanel.costModeling.creation') }}</th>
                <th class="num">{{ t('observabilityPanel.usage.cost') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in scenarioResult.perGroup" :key="row.groupKey || '__ungrouped__'">
                <td>{{ row.groupKey || t('observabilityPanel.costModeling.ungrouped') }}</td>
                <td class="num">{{ formatCost(row.foreground) }}</td>
                <td class="num">{{ formatCost(row.background) }}</td>
                <td class="num">{{ formatCost(row.creation) }}</td>
                <td class="num">{{ formatCost(row.cost) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- Results: per character background noise -->
      <section class="cm-section">
        <h4>{{ t('observabilityPanel.costModeling.byCharacterTitle') }}</h4>
        <div class="cm-scroll">
          <table class="cm-table">
            <thead>
              <tr>
                <th>{{ t('observabilityPanel.usage.byCharacter.character') }}</th>
                <th class="num">{{ t('observabilityPanel.costModeling.activeDays') }}</th>
                <th class="num">{{ t('observabilityPanel.costModeling.background') }}</th>
                <th class="num">{{ t('observabilityPanel.costModeling.backgroundPerDay') }}</th>
                <th class="num">{{ t('observabilityPanel.costModeling.monthlyNoise') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in characterNoise" :key="row.characterId ?? '__unattributed__'">
                <td>{{ characterName(row.characterId) }}</td>
                <td class="num">{{ row.activeDays }}</td>
                <td class="num">{{ formatCost(row.backgroundCost) }}</td>
                <td class="num">{{ formatCost(row.perActiveDay) }}</td>
                <td class="num cm-strong">{{ formatCost(row.monthlyNoise) }}</td>
              </tr>
              <tr v-if="characterNoise.length === 0">
                <td colspan="5" class="empty">{{ t('observabilityPanel.usage.noUsage') }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- Saved scenarios -->
      <section class="cm-section">
        <h4>{{ t('observabilityPanel.costModeling.scenariosTitle') }}</h4>
        <p class="cm-hint">{{ t('observabilityPanel.costModeling.scenariosHint') }}</p>
        <div class="cm-scenario-save">
          <input
            v-model="newScenarioName"
            class="field-input"
            :placeholder="t('observabilityPanel.costModeling.scenarioNamePlaceholder')"
          />
          <UiButton size="sm" :disabled="!newScenarioName.trim()" @click="persistScenario">
            {{ t('observabilityPanel.costModeling.save') }}
          </UiButton>
        </div>
        <ul v-if="scenarioNames.length > 0" class="cm-scenario-list">
          <li v-for="name in scenarioNames" :key="name">
            <span class="cm-scenario-name">{{ name }}</span>
            <button type="button" class="cm-link" @click="applyStoredScenario(name)">
              {{ t('observabilityPanel.costModeling.load') }}
            </button>
            <button type="button" class="cm-link cm-danger" @click="removeStoredScenario(name)">
              {{ t('observabilityPanel.costModeling.delete') }}
            </button>
          </li>
        </ul>
        <p v-else class="cm-hint">{{ t('observabilityPanel.costModeling.noScenarios') }}</p>
      </section>
    </template>
  </section>
</template>

<style scoped>
.cost-modeling {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.cm-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.cm-date {
  width: 150px;
}
.cm-headline {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px;
}
.cm-card {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface, rgba(255, 255, 255, 0.02));
}
.cm-card strong {
  font-size: 18px;
  font-variant-numeric: tabular-nums;
}
.cm-card small {
  font-size: 11px;
  color: var(--color-text-secondary);
}
.cm-card.is-save strong {
  color: var(--color-success, #3fb950);
}
.cm-card.is-cost strong {
  color: var(--color-danger, #f85149);
}
.cm-delta-tag {
  font-size: 11px;
  font-weight: 600;
  margin-left: 4px;
  opacity: 0.85;
}
.metric-label {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.cm-warn {
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--color-warning, #d29922);
  color: var(--color-warning, #d29922);
  font-size: 12px;
}
.cm-warn p {
  margin: 0;
}
.cm-warn p + p {
  margin-top: 4px;
}
.cm-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.cm-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.cm-section h4 {
  margin: 0;
  font-size: 13px;
}
.cm-hint,
.cm-note {
  margin: 0;
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.cm-scroll {
  overflow-x: auto;
}
.cm-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.cm-table th,
.cm-table td {
  padding: 6px 8px;
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  white-space: nowrap;
}
.cm-table th.num,
.cm-table td.num {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.cm-group-label {
  display: block;
}
.cm-group-key {
  display: block;
  font-size: 10px;
  color: var(--color-text-secondary);
}
.cm-model-input {
  width: 200px;
  padding: 4px 6px;
}
.cm-price {
  width: 90px;
  padding: 4px 6px;
  text-align: right;
}
.cm-fallback-row td {
  font-weight: 600;
}
.cm-advanced summary {
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
}
.cm-override-add {
  display: flex;
  gap: 8px;
  align-items: center;
  margin: 6px 0;
}
.cm-remove {
  border: none;
  background: transparent;
  color: var(--color-danger, #f85149);
  font-size: 16px;
  cursor: pointer;
  line-height: 1;
}
.cm-strong {
  font-weight: 700;
  color: var(--color-primary-light);
}
.cm-scenario-save {
  display: flex;
  gap: 8px;
  align-items: center;
}
.cm-scenario-list {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.cm-scenario-list li {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.cm-scenario-name {
  flex: 1;
}
.cm-link {
  border: none;
  background: transparent;
  color: var(--color-primary-light);
  cursor: pointer;
  font-size: 12px;
}
.cm-link.cm-danger {
  color: var(--color-danger, #f85149);
}
.empty {
  color: var(--color-text-secondary);
  padding: 10px 0;
}
.error {
  color: var(--color-danger, #f85149);
  font-size: 12px;
}
</style>
