/**
 * LLM routing cost what-if model for the observability "cost modeling" tab.
 *
 * The usage ledger records the token volume each feature actually consumed
 * over a window. This module re-prices that same volume under a *hypothetical*
 * routing configuration ("scenario") so an operator can answer questions like
 * "if I move proactive_intention off gpt-5.4 onto v4-pro, how much does the
 * 30-day bill drop?" without touching the DB.
 *
 * It is intentionally pure + framework-free (mirrors ``usagePricing.ts``):
 * all reactive/HTTP wiring lives in ``CostModelingPanel.vue``.
 *
 * Price bookkeeping is *shared* with the usage-tab calculator — the same
 * ``PriceBook`` (keyed by capability/provider/model, per-1M-token rates,
 * persisted under ``PRICE_BOOK_STORAGE_KEY``) is reused so an operator never
 * enters a model's price twice.
 */

import {
  type PriceBook,
  computeCustomCost,
  hasPrice,
  priceBookKey,
} from './usagePricing'

/** A provider/model routing target. */
export interface ModelRef {
  providerId: string
  modelId: string
}

/**
 * A routing configuration under test. ``priceBook`` is the shared per-1M
 * rate table; the three routing fields mirror the live backend precedence:
 * a single-feature override wins over its group's assignment, which wins over
 * the global ``fallback`` (the active-model default).
 */
export interface ScenarioRouting {
  /** groupKey → model for whole feature groups (e.g. player_facing_voice). */
  groupAssignments: Record<string, ModelRef>
  /** featureKey → model, a targeted override for one feature. */
  featureOverrides: Record<string, ModelRef>
  /** Global default when neither an override nor a group assignment applies. */
  fallback: ModelRef | null
}

export interface Scenario extends ScenarioRouting {
  priceBook: PriceBook
}

/**
 * Foreground = the chat chain the user directly waits on / sees in real time.
 * Kept explicit (not derived) because "foreground" is a product distinction,
 * not a routing-group one: these keys span player_facing_voice,
 * core_structured_memory and light_observers groups. Source: the chat
 * send → post-turn extraction → persona/idle side-jobs that fire on a user
 * message, plus the busy/away replies that answer one. Calibrated against the
 * 2026-07-10 DB dump where this set summed to ~3% of steady-state LLM cost.
 */
export const FOREGROUND_FEATURE_KEYS: ReadonlySet<string> = new Set([
  'chat',
  'post_turn',
  'persona_extract',
  'busy_reply_decide',
  'chat_assist',
  'image_recognition',
  'idle_drift',
  'chat_repetition_check',
  'nsfw_safe_summary',
  'busy_follow_up',
])

/**
 * Creation = one-shot character-authoring jobs. Excluded from the recurring
 * background-noise statistic because they don't repeat per active day — they
 * fire once when a character is drafted.
 */
export const CREATION_FEATURE_KEYS: ReadonlySet<string> = new Set([
  'character_draft',
  'character_personality_type',
  'character_creation_intake',
])

export type FeatureClass = 'foreground' | 'creation' | 'background'

/** Everything not foreground or creation is recurring background activity
 * (proactive intention, memory consolidation, encounters, schedule plans …).
 * That bucket is the cost body the calibration doc found to be ~97%. */
export function classifyFeature(featureKey: string): FeatureClass {
  if (FOREGROUND_FEATURE_KEYS.has(featureKey)) return 'foreground'
  if (CREATION_FEATURE_KEYS.has(featureKey)) return 'creation'
  return 'background'
}

/** Normalized usage row fed to {@link computeScenarioCost}. Maps 1:1 from
 * either a by-character-feature bucket (``characterId`` set) or a by-feature
 * bucket (``characterId`` null/undefined). */
export interface ScenarioBucket {
  characterId?: string | null
  featureKey: string
  capability: string
  inputTokens: number
  outputTokens: number
}

/** featureKey → groupKey reverse index (from the feature-model-groups API). */
export type FeatureGroupIndex = Record<string, string>

export interface FeatureCost {
  featureKey: string
  groupKey: string | null
  classification: FeatureClass
  capability: string
  model: ModelRef
  inputTokens: number
  outputTokens: number
  cost: number
  /** false when the resolved model has no price entered — its cost is 0 and
   * the UI should flag it so the total isn't silently understated. */
  priced: boolean
}

export interface GroupCost {
  /** Group key, or ``''`` for features that belong to no routing group. */
  groupKey: string
  cost: number
  foreground: number
  background: number
  creation: number
}

export interface CharacterCost {
  characterId: string | null
  total: number
  foreground: number
  background: number
  creation: number
}

export interface ScenarioModelUsage {
  capability: string
  providerId: string
  modelId: string
  priced: boolean
}

export interface ScenarioCostResult {
  total: number
  foreground: number
  background: number
  creation: number
  /** Aggregated per feature (routing is global per feature, so a feature that
   * appears for many characters collapses to one row). Sorted by cost desc. */
  perFeature: FeatureCost[]
  /** Aggregated per routing group. Sorted by cost desc. */
  perGroup: GroupCost[]
  /** Aggregated per character (empty when buckets carry no character id).
   * Sorted by cost desc. */
  perCharacter: CharacterCost[]
  /** Distinct resolved (capability, provider, model) targets — the set of
   * price rows the UI must let the operator fill in. */
  models: ScenarioModelUsage[]
  /** true when at least one resolved model is missing a price. */
  hasUnpriced: boolean
}

const EMPTY_MODEL: ModelRef = { providerId: '', modelId: '' }

function isRealModel(model: ModelRef | null | undefined): model is ModelRef {
  return !!model && !!(model.providerId || model.modelId)
}

/**
 * Resolve the model a feature routes to under ``scenario``, mirroring the
 * backend precedence: featureOverride → group assignment → global fallback.
 * Returns an empty ref when nothing is configured (its cost is then 0/unpriced).
 */
export function resolveModelForFeature(
  featureKey: string,
  scenario: ScenarioRouting,
  featureToGroup: FeatureGroupIndex,
): ModelRef {
  const override = scenario.featureOverrides[featureKey]
  if (isRealModel(override)) return override
  const groupKey = featureToGroup[featureKey]
  if (groupKey) {
    const groupModel = scenario.groupAssignments[groupKey]
    if (isRealModel(groupModel)) return groupModel
  }
  if (isRealModel(scenario.fallback)) return scenario.fallback
  return EMPTY_MODEL
}

/**
 * Re-price ``buckets`` under ``scenario``. Cost of each feature = its recorded
 * input/output token volume × the *resolved* model's per-1M price. A model
 * with no price contributes 0 and is flagged via ``priced``/``hasUnpriced``.
 */
export function computeScenarioCost(
  buckets: readonly ScenarioBucket[],
  scenario: Scenario,
  featureToGroup: FeatureGroupIndex,
): ScenarioCostResult {
  const featureAcc = new Map<string, FeatureCost>()
  const groupAcc = new Map<string, GroupCost>()
  const characterAcc = new Map<string, CharacterCost>()
  const modelAcc = new Map<string, ScenarioModelUsage>()

  for (const bucket of buckets) {
    const model = resolveModelForFeature(
      bucket.featureKey,
      scenario,
      featureToGroup,
    )
    const entry = scenario.priceBook[
      priceBookKey(bucket.capability, model.providerId, model.modelId)
    ]
    const priced = hasPrice(entry)
    const cost = computeCustomCost(bucket.inputTokens, bucket.outputTokens, entry)
    const classification = classifyFeature(bucket.featureKey)
    const groupKey = featureToGroup[bucket.featureKey] ?? null

    // Per feature (collapse repeats across characters).
    const fPrev = featureAcc.get(bucket.featureKey)
    if (fPrev) {
      fPrev.inputTokens += bucket.inputTokens
      fPrev.outputTokens += bucket.outputTokens
      fPrev.cost += cost
      fPrev.priced = fPrev.priced && priced
    } else {
      featureAcc.set(bucket.featureKey, {
        featureKey: bucket.featureKey,
        groupKey,
        classification,
        capability: bucket.capability,
        model,
        inputTokens: bucket.inputTokens,
        outputTokens: bucket.outputTokens,
        cost,
        priced,
      })
    }

    // Per group ('' bucket for ungrouped features).
    const gKey = groupKey ?? ''
    const gPrev = groupAcc.get(gKey) ?? {
      groupKey: gKey,
      cost: 0,
      foreground: 0,
      background: 0,
      creation: 0,
    }
    gPrev.cost += cost
    gPrev[classification] += cost
    groupAcc.set(gKey, gPrev)

    // Per character (only meaningful when buckets carry ids).
    if (bucket.characterId !== undefined) {
      const cKey = bucket.characterId ?? '__unattributed__'
      const cPrev = characterAcc.get(cKey) ?? {
        characterId: bucket.characterId ?? null,
        total: 0,
        foreground: 0,
        background: 0,
        creation: 0,
      }
      cPrev.total += cost
      cPrev[classification] += cost
      characterAcc.set(cKey, cPrev)
    }

    // Distinct resolved model set.
    if (isRealModel(model)) {
      const mKey = priceBookKey(bucket.capability, model.providerId, model.modelId)
      if (!modelAcc.has(mKey)) {
        modelAcc.set(mKey, {
          capability: bucket.capability,
          providerId: model.providerId,
          modelId: model.modelId,
          priced,
        })
      }
    }
  }

  let total = 0
  let foreground = 0
  let background = 0
  let creation = 0
  for (const f of featureAcc.values()) {
    total += f.cost
    if (f.classification === 'foreground') foreground += f.cost
    else if (f.classification === 'creation') creation += f.cost
    else background += f.cost
  }

  const models = [...modelAcc.values()]
  return {
    total,
    foreground,
    background,
    creation,
    perFeature: [...featureAcc.values()].sort((a, b) => b.cost - a.cost),
    perGroup: [...groupAcc.values()].sort((a, b) => b.cost - a.cost),
    perCharacter: [...characterAcc.values()].sort((a, b) => b.total - a.total),
    models,
    hasUnpriced: models.some((m) => !m.priced),
  }
}

const DAYS_PER_MONTH = 30

export interface CharacterNoise {
  characterId: string | null
  backgroundCost: number
  activeDays: number
  /** background cost per active day (0 when the character has no active day). */
  perActiveDay: number
  /** perActiveDay × 30 — a comparable monthly background-noise figure that is
   * independent of the query window length. */
  monthlyNoise: number
}

/**
 * Per-character background noise: background cost ÷ active days, then ×30 for a
 * monthly figure. Matches the calibration doc's "$/active day, ≈$24/mo for a
 * main character, ≈$12/mo for a typical active one".
 */
export function computeCharacterNoise(
  perCharacter: readonly CharacterCost[],
  activeDaysById: Record<string, number>,
): CharacterNoise[] {
  return perCharacter.map((c) => {
    const key = c.characterId ?? '__unattributed__'
    const activeDays = Math.max(0, activeDaysById[key] ?? 0)
    const perActiveDay = activeDays > 0 ? c.background / activeDays : 0
    return {
      characterId: c.characterId,
      backgroundCost: c.background,
      activeDays,
      perActiveDay,
      monthlyNoise: perActiveDay * DAYS_PER_MONTH,
    }
  }).sort((a, b) => b.monthlyNoise - a.monthlyNoise)
}

/** Build the featureKey → groupKey reverse index from the feature-model-groups
 * API payload (``groups[].members[].key``). */
export function buildFeatureGroupIndex(
  groups: readonly { key: string; members: readonly { key: string }[] }[],
): FeatureGroupIndex {
  const index: FeatureGroupIndex = {}
  for (const group of groups) {
    for (const member of group.members) index[member.key] = group.key
  }
  return index
}

// ----------------------------------------------------------------------
// Named-scenario persistence. Only the routing is stored — the price book is
// the shared pool from ``usagePricing.ts`` and must NOT be duplicated here.
// ----------------------------------------------------------------------

export const SCENARIO_STORAGE_KEY = 'yuralume.costModeling.scenarios.v1'

export type ScenarioLibrary = Record<string, ScenarioRouting>

function isModelRef(value: unknown): value is ModelRef {
  return (
    typeof value === 'object'
    && value !== null
    && typeof (value as ModelRef).providerId === 'string'
    && typeof (value as ModelRef).modelId === 'string'
  )
}

function sanitizeModelMap(value: unknown): Record<string, ModelRef> {
  const out: Record<string, ModelRef> = {}
  if (typeof value !== 'object' || value === null) return out
  for (const [key, model] of Object.entries(value as Record<string, unknown>)) {
    if (isModelRef(model)) out[key] = { providerId: model.providerId, modelId: model.modelId }
  }
  return out
}

function isScenarioRouting(value: unknown): value is ScenarioRouting {
  return typeof value === 'object' && value !== null
}

/** Coerce arbitrary stored JSON into a valid routing, dropping bad leaves. */
export function sanitizeRouting(value: unknown): ScenarioRouting {
  const raw = (value ?? {}) as Record<string, unknown>
  const fallback = raw.fallback
  return {
    groupAssignments: sanitizeModelMap(raw.groupAssignments),
    featureOverrides: sanitizeModelMap(raw.featureOverrides),
    fallback: isModelRef(fallback)
      ? { providerId: fallback.providerId, modelId: fallback.modelId }
      : null,
  }
}

/** Load the named-scenario library, tolerating missing / corrupt storage. */
export function loadScenarios(
  storage: Pick<Storage, 'getItem'> | null | undefined,
): ScenarioLibrary {
  if (!storage) return {}
  try {
    const raw = storage.getItem(SCENARIO_STORAGE_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (typeof parsed !== 'object' || parsed === null) return {}
    const library: ScenarioLibrary = {}
    for (const [name, routing] of Object.entries(parsed as Record<string, unknown>)) {
      if (isScenarioRouting(routing)) library[name] = sanitizeRouting(routing)
    }
    return library
  } catch {
    return {}
  }
}

function persistScenarios(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  library: ScenarioLibrary,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(SCENARIO_STORAGE_KEY, JSON.stringify(library))
    return true
  } catch {
    return false
  }
}

/** Add or replace one named scenario, returning the updated library. */
export function saveScenario(
  storage: Pick<Storage, 'getItem' | 'setItem'> | null | undefined,
  name: string,
  routing: ScenarioRouting,
): ScenarioLibrary {
  const trimmed = name.trim()
  if (!trimmed) return storage ? loadScenarios(storage) : {}
  const library = storage ? loadScenarios(storage) : {}
  library[trimmed] = sanitizeRouting(routing)
  persistScenarios(storage, library)
  return library
}

/** Delete one named scenario, returning the updated library. */
export function deleteScenario(
  storage: Pick<Storage, 'getItem' | 'setItem'> | null | undefined,
  name: string,
): ScenarioLibrary {
  const library = storage ? loadScenarios(storage) : {}
  if (name in library) {
    delete library[name]
    persistScenarios(storage, library)
  }
  return library
}
