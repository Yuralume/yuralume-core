import { describe, expect, it } from 'vitest'

import { priceBookKey, type PriceBook } from '@/utils/usagePricing'
import {
  type Scenario,
  type ScenarioBucket,
  CREATION_FEATURE_KEYS,
  FOREGROUND_FEATURE_KEYS,
  SCENARIO_STORAGE_KEY,
  buildFeatureGroupIndex,
  classifyFeature,
  computeCharacterNoise,
  computeScenarioCost,
  deleteScenario,
  loadScenarios,
  resolveModelForFeature,
  sanitizeRouting,
  saveScenario,
} from '@/utils/costModeling'

// Verified public pricing (per 1M tokens) from the 2026-07-10 calibration.
const PRICES: PriceBook = {
  [priceBookKey('llm', 'openai', 'gpt-5.4')]: { inputPerMillion: '2.5', outputPerMillion: '15' },
  [priceBookKey('llm', 'openrouter', 'deepseek/deepseek-v4-pro')]:
    { inputPerMillion: '0.435', outputPerMillion: '0.87' },
  [priceBookKey('llm', 'openai', 'gpt-5.4-mini')]: { inputPerMillion: '0.75', outputPerMillion: '4.5' },
}

const FEATURE_TO_GROUP = buildFeatureGroupIndex([
  { key: 'player_facing_voice', members: [{ key: 'chat' }] },
  { key: 'high_reasoning_gates', members: [{ key: 'proactive_intention' }] },
])

const BASELINE: Scenario = {
  groupAssignments: {
    player_facing_voice: { providerId: 'openrouter', modelId: 'deepseek/deepseek-v4-pro' },
    high_reasoning_gates: { providerId: 'openai', modelId: 'gpt-5.4' },
  },
  featureOverrides: {},
  fallback: { providerId: 'openai', modelId: 'gpt-5.4-mini' },
  priceBook: PRICES,
}

const BUCKETS: ScenarioBucket[] = [
  { characterId: 'char-1', featureKey: 'chat', capability: 'llm', inputTokens: 1_000_000, outputTokens: 200_000 },
  { characterId: 'char-1', featureKey: 'proactive_intention', capability: 'llm', inputTokens: 2_000_000, outputTokens: 300_000 },
  { characterId: 'char-2', featureKey: 'chat', capability: 'llm', inputTokens: 500_000, outputTokens: 100_000 },
]

function fakeStorage(initial: Record<string, string> = {}) {
  const values = new Map(Object.entries(initial))
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

describe('costModeling classification', () => {
  it('splits foreground / creation / background by feature key', () => {
    expect(classifyFeature('chat')).toBe('foreground')
    expect(classifyFeature('busy_follow_up')).toBe('foreground')
    expect(classifyFeature('character_draft')).toBe('creation')
    expect(classifyFeature('proactive_intention')).toBe('background')
    // Any unknown key is recurring background by default.
    expect(classifyFeature('some_future_feature')).toBe('background')
  })

  it('keeps foreground and creation sets disjoint', () => {
    for (const key of FOREGROUND_FEATURE_KEYS) {
      expect(CREATION_FEATURE_KEYS.has(key)).toBe(false)
    }
  })
})

describe('resolveModelForFeature precedence', () => {
  it('honours override > group > fallback', () => {
    const scenario = {
      ...BASELINE,
      featureOverrides: {
        proactive_intention: { providerId: 'openrouter', modelId: 'deepseek/deepseek-v4-pro' },
      },
    }
    // override wins over the group's gpt-5.4
    expect(resolveModelForFeature('proactive_intention', scenario, FEATURE_TO_GROUP))
      .toEqual({ providerId: 'openrouter', modelId: 'deepseek/deepseek-v4-pro' })
    // group wins over the mini fallback
    expect(resolveModelForFeature('chat', scenario, FEATURE_TO_GROUP))
      .toEqual({ providerId: 'openrouter', modelId: 'deepseek/deepseek-v4-pro' })
    // no override + no group → fallback
    expect(resolveModelForFeature('unknown_feature', scenario, FEATURE_TO_GROUP))
      .toEqual({ providerId: 'openai', modelId: 'gpt-5.4-mini' })
  })
})

describe('computeScenarioCost', () => {
  it('re-prices recorded token volume under the baseline routing', () => {
    const result = computeScenarioCost(BUCKETS, BASELINE, FEATURE_TO_GROUP)
    // chat (v4-pro): (1.5M*0.435 + 0.3M*0.87)/1M = 0.6525 + 0.261 = 0.9135
    // proactive (gpt-5.4): 2*2.5 + 0.3*15 = 9.5
    expect(result.total).toBeCloseTo(10.4135, 6)
    expect(result.foreground).toBeCloseTo(0.9135, 6)
    expect(result.background).toBeCloseTo(9.5, 6)
    expect(result.creation).toBe(0)

    // Foreground ≈ 8.8% here, mirroring the calibration's "foreground is a
    // small slice, background is the body" finding.
    const fgRatio = result.foreground / result.total
    expect(fgRatio).toBeLessThan(0.1)

    const chat = result.perFeature.find((f) => f.featureKey === 'chat')!
    expect(chat.model).toEqual({ providerId: 'openrouter', modelId: 'deepseek/deepseek-v4-pro' })
    expect(chat.inputTokens).toBe(1_500_000)
    expect(chat.cost).toBeCloseTo(0.9135, 6)

    // proactive_intention is the single largest line, echoing the DB dump.
    expect(result.perFeature[0].featureKey).toBe('proactive_intention')
    expect(result.hasUnpriced).toBe(false)
  })

  it('quantifies a background-degrade what-if (proactive → v4-pro)', () => {
    const scenario = {
      ...BASELINE,
      featureOverrides: {
        proactive_intention: { providerId: 'openrouter', modelId: 'deepseek/deepseek-v4-pro' },
      },
    }
    const baseline = computeScenarioCost(BUCKETS, BASELINE, FEATURE_TO_GROUP)
    const degraded = computeScenarioCost(BUCKETS, scenario, FEATURE_TO_GROUP)
    // proactive on v4-pro: 2M*0.435 + 0.3M*0.87 = 0.87 + 0.261 = 1.131
    expect(degraded.background).toBeCloseTo(1.131, 6)
    expect(baseline.total - degraded.total).toBeCloseTo(8.369, 6)
  })

  it('flags resolved models with no entered price', () => {
    const scenario = { ...BASELINE, priceBook: {} }
    const result = computeScenarioCost(BUCKETS, scenario, FEATURE_TO_GROUP)
    expect(result.total).toBe(0)
    expect(result.hasUnpriced).toBe(true)
    expect(result.models.every((m) => !m.priced)).toBe(true)
  })

  it('aggregates per character', () => {
    const result = computeScenarioCost(BUCKETS, BASELINE, FEATURE_TO_GROUP)
    const char1 = result.perCharacter.find((c) => c.characterId === 'char-1')!
    // char-1 chat (1M/0.2M v4-pro) = 0.435 + 0.174 = 0.609; + proactive 9.5
    expect(char1.foreground).toBeCloseTo(0.609, 6)
    expect(char1.background).toBeCloseTo(9.5, 6)
    expect(char1.total).toBeCloseTo(10.109, 6)
    // highest-cost character first
    expect(result.perCharacter[0].characterId).toBe('char-1')
  })
})

describe('computeCharacterNoise', () => {
  it('derives $/active-day and ×30 monthly background noise', () => {
    const result = computeScenarioCost(BUCKETS, BASELINE, FEATURE_TO_GROUP)
    const noise = computeCharacterNoise(result.perCharacter, { 'char-1': 20, 'char-2': 10 })
    const char1 = noise.find((n) => n.characterId === 'char-1')!
    // background 9.5 / 20 active days = 0.475 /day ; ×30 = 14.25 /mo
    expect(char1.perActiveDay).toBeCloseTo(0.475, 6)
    expect(char1.monthlyNoise).toBeCloseTo(14.25, 6)
  })

  it('never divides by zero active days', () => {
    const noise = computeCharacterNoise(
      [{ characterId: 'x', total: 5, foreground: 1, background: 4, creation: 0 }],
      {},
    )
    expect(noise[0].perActiveDay).toBe(0)
    expect(noise[0].monthlyNoise).toBe(0)
  })
})

describe('scenario persistence', () => {
  it('round-trips a named scenario (routing only, no price book)', () => {
    const storage = fakeStorage()
    const routing = {
      groupAssignments: { high_reasoning_gates: { providerId: 'openrouter', modelId: 'deepseek/deepseek-v4-pro' } },
      featureOverrides: {},
      fallback: { providerId: 'openai', modelId: 'gpt-5.4-mini' },
    }
    saveScenario(storage, 'hosted-degrade', routing)
    const loaded = loadScenarios(storage)
    expect(loaded['hosted-degrade']).toEqual(routing)

    const after = deleteScenario(storage, 'hosted-degrade')
    expect(after['hosted-degrade']).toBeUndefined()
  })

  it('sanitizes corrupt routing and payloads', () => {
    // bad model leaves dropped, missing maps default empty
    expect(sanitizeRouting({ groupAssignments: { g: { providerId: 'p' } }, fallback: 42 }))
      .toEqual({ groupAssignments: {}, featureOverrides: {}, fallback: null })
    // corrupt JSON / wrong types → empty library
    expect(loadScenarios(fakeStorage({ [SCENARIO_STORAGE_KEY]: '{not json' }))).toEqual({})
    expect(loadScenarios(null)).toEqual({})
  })

  it('ignores blank scenario names', () => {
    const storage = fakeStorage()
    saveScenario(storage, '   ', {
      groupAssignments: {}, featureOverrides: {}, fallback: null,
    })
    expect(loadScenarios(storage)).toEqual({})
  })
})
