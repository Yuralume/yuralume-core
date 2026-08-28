/**
 * NSFW mode target 的「明確關閉圖片生成」選項（AM3）。
 *
 * 有決定的部分抽在 `@/utils/nsfwModeTarget`（sentinel ↔ null 映射與可存
 * 條件），這裡是真正的閘。元件本身（select 綁定、hint 顯示）沒有 DOM
 * test infra（本 repo 無 jsdom / @vue/test-utils，慣例見
 * lightbox.test.ts），只能做原始碼掃描釘住接線：sentinel 只能經由映射
 * 函式進出，絕不能直接出現在 API payload。
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import {
  NSFW_IMAGE_GENERATION_OFF,
  hasSelectableNsfwTarget,
  imageProfileIdForSave,
  imageProfileSelectionFromTarget,
} from '@/utils/nsfwModeTarget'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { messages as zhTW } from '@/i18n/locales/zh-TW'

describe('nsfwModeTarget selection mapping', () => {
  it('maps a stored null (image generation off) onto the off option', () => {
    expect(imageProfileSelectionFromTarget(null))
      .toBe(NSFW_IMAGE_GENERATION_OFF)
    expect(imageProfileSelectionFromTarget('anime_nsfw')).toBe('anime_nsfw')
  })

  it('maps the off option back to null for the API payload', () => {
    expect(imageProfileIdForSave(NSFW_IMAGE_GENERATION_OFF)).toBeNull()
    expect(imageProfileIdForSave('anime_nsfw')).toBe('anime_nsfw')
  })

  it('round-trips both directions without inventing a sentinel id', () => {
    for (const stored of [null, 'anime_nsfw']) {
      expect(imageProfileIdForSave(imageProfileSelectionFromTarget(stored)))
        .toBe(stored)
    }
  })

  it('requires LLM provider and model, image column is profile-or-off', () => {
    const base = {
      llmProviderId: 'lmstudio',
      llmModelId: 'local-nsfw',
      imageProfileSelection: 'anime_nsfw',
    }
    expect(hasSelectableNsfwTarget(base)).toBe(true)
    expect(hasSelectableNsfwTarget({
      ...base, imageProfileSelection: NSFW_IMAGE_GENERATION_OFF,
    })).toBe(true)
    // Off is an explicit choice — an untouched blank stays unsaveable.
    expect(hasSelectableNsfwTarget({ ...base, imageProfileSelection: '' }))
      .toBe(false)
    expect(hasSelectableNsfwTarget({ ...base, llmProviderId: '' })).toBe(false)
    expect(hasSelectableNsfwTarget({ ...base, llmModelId: '' })).toBe(false)
  })
})

describe('NsfwModeTargetSetting wiring (source scan)', () => {
  const source = readFileSync(
    fileURLToPath(new URL(
      '../src/components/NsfwModeTargetSetting.vue', import.meta.url,
    )),
    'utf8',
  )

  it('renders the explicit off option and its consequence hint', () => {
    expect(source).toContain(':value="NSFW_IMAGE_GENERATION_OFF"')
    expect(source).toContain('nsfwModeTargetSetting.options.imageGenerationOff')
    expect(source).toContain('nsfwModeTargetSetting.hints.imageDisabled')
  })

  it('routes the payload and target through the mapping functions', () => {
    expect(source).toContain(
      'imageProfileIdForSave(selectedImageProfileId.value)',
    )
    expect(source).toContain(
      'imageProfileSelectionFromTarget(nextTarget.image_profile_id)',
    )
  })

  it('keeps the image select usable when no profiles are registered', () => {
    // With zero registered profiles the only valid choice is "off" — the
    // select must not be disabled on an empty profile list.
    expect(source).not.toContain('saving || imageProfiles.length === 0')
  })

  it('wires the target reasoning posture through the shared editor (AM2)', () => {
    // Same compact editor as feature/group rows — no bespoke fields.
    expect(source).toContain('ReasoningOverrideFields')
    expect(source).toContain('v-model="selectedReasoning"')
    // Load side: the stored posture rehydrates the editor.
    expect(source).toContain('selectedReasoning.value = nextTarget.reasoning ?? null')
    // Save side: an all-default posture collapses to null before the
    // payload, mirroring the backend write-side normalisation.
    expect(source).toContain('hasReasoningOverride(selectedReasoning.value)')
  })
})

describe('NsfwModeTargetSetting i18n copy', () => {
  const locales = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

  it('ships the off option and hint in all three locales', () => {
    for (const [name, catalogue] of Object.entries(locales)) {
      const block = (catalogue as Record<string, any>).nsfwModeTargetSetting
      expect(block?.options?.imageGenerationOff, name).toBeTruthy()
      expect(block?.hints?.imageDisabled, name).toBeTruthy()
    }
  })
})
