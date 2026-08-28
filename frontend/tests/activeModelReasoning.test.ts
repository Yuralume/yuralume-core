/**
 * active_model 推理覆寫（AM2）——主要模型列掛上與 group 列相同的
 * ReasoningOverrideFields，payload / 回填走 hasReasoningOverride 正規化。
 *
 * 元件無 DOM test infra（本 repo 無 jsdom / @vue/test-utils，慣例見
 * nsfwModeTargetSetting.test.ts），以原始碼掃描釘住接線；API 形狀由
 * TypeScript 型別在 build（vue-tsc）時把關。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import type { ActiveModelPreference } from '@/utils/api/system'

describe('FeatureModelsPicker active-row reasoning wiring (source scan)', () => {
  const source = readFileSync(
    fileURLToPath(new URL(
      '../src/components/FeatureModelsPicker.vue', import.meta.url,
    )),
    'utf8',
  )

  it('mounts the shared reasoning editor on the active row', () => {
    expect(source).toContain(':model-value="activeReasoning"')
    expect(source).toContain('onActiveReasoningChange')
  })

  it('round-trips the posture through load and save', () => {
    // Load: rehydrate from GET.
    expect(source).toContain('activeReasoning.value = activePref?.reasoning ?? null')
    // Save: all-default collapses to null before the payload, and the
    // echoed (normalised) result replaces local state.
    expect(source).toContain('hasReasoningOverride(activeReasoning.value)')
    expect(source).toContain('activeReasoning.value = activeResult.reasoning ?? null')
  })
})

describe('ActiveModelPreference API shape', () => {
  it('accepts an optional reasoning posture', () => {
    const preference: ActiveModelPreference = {
      provider_id: 'openai',
      model_id: 'gpt-5.2',
      reasoning: {
        disable_reasoning: false,
        reasoning_effort: 'high',
        thinking_budget_tokens: null,
      },
    }
    expect(preference.reasoning?.reasoning_effort).toBe('high')
  })
})
