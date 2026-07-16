import { describe, expect, it } from 'vitest'

import { i18n } from '@/i18n'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import {
  CAPABILITY_LABEL_NAMESPACE,
  FEATURE_KEY_LABEL_NAMESPACE,
  IMAGE_FEATURE_KEY_LABEL_NAMESPACE,
  PROVIDER_DISPLAY_NAME_NAMESPACE,
  PROVIDER_FIELD_NAMESPACE,
  VIDEO_FEATURE_KEY_LABEL_NAMESPACE,
  featureKeyLabel,
  providerConnectionLabel,
  providerDisplayNameLabel,
  providerFieldHint,
  providerFieldLabel,
  providerFieldPlaceholder,
} from '@/utils/catalogLabels'

const CAPABILITY_KEYS = ['llm', 'embedding', 'image', 'video', 'tts', 'search', 'cloud'] as const

// Authoritative key lists mirrored from the backend so the i18n gate
// stays honest: if the backend adds a feature key or provider field,
// these lists must grow too, and the "every key is translated" checks
// below turn red until all three locales are filled in.
//
// Source of truth:
//   - GLOBAL_FEATURE_KEYS in
//     src/kokoro_link/application/services/feature_keys.py
//   - ProviderFieldSpec.key across
//     src/kokoro_link/infrastructure/provider_settings/catalog.py
const GLOBAL_FEATURE_KEYS = [
  'chat',
  'image_recognition',
  'post_turn',
  'goal_review',
  'schedule_plan',
  'arc_plan',
  'arc_season_decide',
  'arc_beat_recheck',
  'arc_scene_write',
  'arc_completion_memory',
  'story_expand',
  'memory_consolidate',
  'dialogue_summary',
  'prompt_rewrite',
  'prompt_material_digest',
  'novelty_gate',
  'register_profile',
  'character_draft',
  'character_personality_type',
  'character_creation_intake',
  'arc_template_intake',
  'arc_adapt',
  'arc_continuation_draft',
  'feed_compose',
  'feed_comment_reply',
  'activity_aftermath',
  'idle_drift',
  'busy_reply_decide',
  'scene_access',
  'busy_follow_up',
  'scheduled_promise',
  'proactive_intention',
  'tts_translate',
  'card_translate',
  'arc_template_translate',
  'story_seed_translate',
  'sillytavern_normalize',
  'memoir_localize',
  'fusion_story',
  'fusion_story_critic',
  'branching_drama',
  'branching_drama_critic',
  'chat_repetition_check',
  'chat_assist',
  'persona_extract',
  'persona_dream',
  'persona_projection',
  'persona_curiosity',
  'address_preference_observer',
  'experiment_analysis',
  'character_encounter_plan',
  'character_encounter_dialogue',
  'character_encounter_reflect',
  'peer_knowledge_consolidate',
] as const

// Every unique ProviderFieldSpec.key. Labels are always translated;
// placeholders only when the backend spec ships a non-empty one.
const PROVIDER_FIELD_KEYS = [
  'anthropic_version',
  'api_key',
  'base_url',
  'checkpoint',
  'default_model',
  'disable_reasoning',
  'embedding_dimension',
  'embedding_model',
  'extra_request_params',
  'image_model',
  'lora_dir',
  'max_results',
  'max_tokens',
  'reasoning_effort',
  'request_dimensions',
  'response_format',
  'search_depth',
  'searxng_base_url',
  'server',
  'strip_think_tags',
  'supports_vision',
  'thinking_budget_tokens',
  'timeout_seconds',
  'tts_model',
  'voice_id',
  'workflow_file',
] as const

// Field keys whose backend spec has a non-empty placeholder (checkbox
// fields carry no placeholder, so they only need a `.label`).
const PROVIDER_FIELD_KEYS_WITH_PLACEHOLDER = [
  'anthropic_version',
  'api_key',
  'base_url',
  'checkpoint',
  'default_model',
  'embedding_dimension',
  'embedding_model',
  'extra_request_params',
  'image_model',
  'lora_dir',
  'max_results',
  'max_tokens',
  'reasoning_effort',
  'response_format',
  'search_depth',
  'searxng_base_url',
  'server',
  'thinking_budget_tokens',
  'timeout_seconds',
  'tts_model',
  'voice_id',
  'workflow_file',
] as const

// Field keys whose backend spec ships a persistent, non-empty hint.
const PROVIDER_FIELD_KEYS_WITH_HINT = ['searxng_base_url'] as const

const LOCALES = [
  ['zh-TW', zhTW],
  ['en-US', enUS],
  ['ja-JP', jaJP],
] as const

// IMAGE_FEATURE_KEYS / VIDEO_FEATURE_KEYS in feature_keys.py. Same
// FEATURE_LABELS family as the LLM routing chips, but rendered in the
// image / video profile pickers under their own namespaces.
const IMAGE_FEATURE_KEYS = [
  'image_chat_tool',
  'image_portrait',
  'image_feed',
] as const

const VIDEO_FEATURE_KEYS = ['video_feed'] as const

function nested(root: Record<string, unknown>, path: string): unknown {
  return path.split('.').reduce<unknown>((acc, part) => {
    if (acc && typeof acc === 'object') {
      return (acc as Record<string, unknown>)[part]
    }
    return undefined
  }, root)
}

describe('featureKeyLabel', () => {
  const t = (key: string, fallback: string) => i18n.global.t(key, fallback)

  it('routes a known feature chip through the featureKeys namespace', () => {
    i18n.global.locale.value = 'en-US'
    // Backend still ships the Chinese label; the English UI must not
    // render it.
    const label = featureKeyLabel(t, { key: 'chat', label: '聊天主回覆' })
    expect(label).not.toBe('聊天主回覆')
    expect(label).toBe(nested(enUS, `${FEATURE_KEY_LABEL_NAMESPACE}.chat`))
  })

  it('falls back to the backend label when the key is unknown', () => {
    i18n.global.locale.value = 'en-US'
    const label = featureKeyLabel(t, {
      key: 'brand_new_feature_key',
      label: '全新功能後端字串',
    })
    expect(label).toBe('全新功能後端字串')
  })

  it('falls back to the key itself when the backend label is empty', () => {
    const label = featureKeyLabel(t, { key: 'unmapped_key', label: '' })
    expect(label).toBe('unmapped_key')
  })

  it('routes an image feature key through the image picker namespace', () => {
    i18n.global.locale.value = 'en-US'
    const label = featureKeyLabel(
      t,
      { key: 'image_chat_tool', label: '生圖：聊天工具' },
      IMAGE_FEATURE_KEY_LABEL_NAMESPACE,
    )
    expect(label).not.toBe('生圖：聊天工具')
    expect(label).toBe(
      nested(enUS, `${IMAGE_FEATURE_KEY_LABEL_NAMESPACE}.image_chat_tool`),
    )
  })

  it('routes a video feature key through the video picker namespace', () => {
    i18n.global.locale.value = 'ja-JP'
    const label = featureKeyLabel(
      t,
      { key: 'video_feed', label: '短影片：動態貼文' },
      VIDEO_FEATURE_KEY_LABEL_NAMESPACE,
    )
    expect(label).not.toBe('短影片：動態貼文')
    expect(label).toBe(
      nested(jaJP, `${VIDEO_FEATURE_KEY_LABEL_NAMESPACE}.video_feed`),
    )
  })

  it('falls back to the backend label for an unknown image/video key', () => {
    const label = featureKeyLabel(
      t,
      { key: 'brand_new_media_key', label: '媒體後端字串' },
      IMAGE_FEATURE_KEY_LABEL_NAMESPACE,
    )
    expect(label).toBe('媒體後端字串')
  })
})

describe('providerFieldLabel / providerFieldPlaceholder', () => {
  const t = (key: string, fallback: string) => i18n.global.t(key, fallback)

  it('routes a known field through the providerFields namespace', () => {
    i18n.global.locale.value = 'zh-TW'
    // Backend ships an English label; the Chinese UI must not render it.
    const label = providerFieldLabel(t, {
      key: 'api_key',
      label: 'API key',
      placeholder: 'sk-...',
    })
    expect(label).toBe(nested(zhTW, `${PROVIDER_FIELD_NAMESPACE}.api_key.label`))
  })

  it('falls back to the backend label when the field key is unknown', () => {
    const label = providerFieldLabel(t, {
      key: 'brand_new_field',
      label: 'Backend English label',
      placeholder: '',
    })
    expect(label).toBe('Backend English label')
  })

  it('keeps empty placeholders empty without a namespace lookup', () => {
    const placeholder = providerFieldPlaceholder(t, {
      key: 'supports_vision',
      label: 'Supports vision',
      placeholder: '',
    })
    expect(placeholder).toBe('')
  })

  it('falls back to the backend placeholder when the field key is unknown', () => {
    const placeholder = providerFieldPlaceholder(t, {
      key: 'brand_new_field',
      label: 'X',
      placeholder: 'backend placeholder',
    })
    expect(placeholder).toBe('backend placeholder')
  })
})

describe('providerFieldHint', () => {
  const t = (key: string, fallback: string) => i18n.global.t(key, fallback)

  it('routes a known field hint through the providerFields namespace', () => {
    i18n.global.locale.value = 'ja-JP'
    const hint = providerFieldHint(t, {
      key: 'searxng_base_url',
      label: 'Base URL (SearXNG instance root)',
      placeholder: 'https://searxng.example.com',
      hint: 'English backend hint',
    })
    expect(hint).not.toBe('English backend hint')
    expect(hint).toBe(nested(jaJP, `${PROVIDER_FIELD_NAMESPACE}.searxng_base_url.hint`))
  })

  it('keeps a blank hint blank without a namespace lookup', () => {
    const hint = providerFieldHint(t, {
      key: 'base_url',
      label: 'Base URL',
      placeholder: 'https://api.example.com/v1',
    })
    expect(hint).toBe('')
  })

  it('falls back to the backend hint when the field key is unknown', () => {
    const hint = providerFieldHint(t, {
      key: 'brand_new_field',
      label: 'X',
      placeholder: '',
      hint: 'backend hint text',
    })
    expect(hint).toBe('backend hint text')
  })
})

describe('providerConnectionLabel', () => {
  const t = (key: string, fallback: string) => i18n.global.t(key, fallback)

  it('re-localizes a frozen zh capability suffix into the current UI locale', () => {
    i18n.global.locale.value = 'en-US'
    // Persisted in a zh session as "OpenAI — 生圖"; an English admin/player
    // must not see the Chinese suffix.
    expect(providerConnectionLabel(t, 'OpenAI — 生圖')).toBe('OpenAI — Image')
  })

  it('re-localizes a frozen en capability suffix back into zh', () => {
    i18n.global.locale.value = 'zh-TW'
    expect(providerConnectionLabel(t, 'OpenAI — Image')).toBe('OpenAI — 生圖')
  })

  it('leaves a hand-edited custom label untouched', () => {
    i18n.global.locale.value = 'en-US'
    expect(providerConnectionLabel(t, 'My studio comfy box')).toBe('My studio comfy box')
    expect(providerConnectionLabel(t, 'OpenAI — my special gpt')).toBe('OpenAI — my special gpt')
  })

  it('passes through a label with no capability separator', () => {
    i18n.global.locale.value = 'en-US'
    expect(providerConnectionLabel(t, 'OpenAI')).toBe('OpenAI')
    expect(providerConnectionLabel(t, '')).toBe('')
  })

  // Drift guard: every shipped localized capability label must be recognised
  // by the reverse map, or an auto-composed suffix in that locale would leak.
  it.each(LOCALES)('recognizes every %s capability label', (_name, catalog) => {
    i18n.global.locale.value = 'en-US'
    for (const key of CAPABILITY_KEYS) {
      const value = nested(catalog, `${CAPABILITY_LABEL_NAMESPACE}.${key}`) as string
      const enValue = nested(enUS, `${CAPABILITY_LABEL_NAMESPACE}.${key}`) as string
      expect(
        providerConnectionLabel(t, `Provider — ${value}`),
        `capability "${value}" not recognized by CAPABILITY_LABEL_TO_KEY`,
      ).toBe(`Provider — ${enValue}`)
    }
  })
})

describe('providerDisplayNameLabel', () => {
  const t = (key: string, fallback: string) => i18n.global.t(key, fallback)

  it('localizes a descriptive provider display name', () => {
    i18n.global.locale.value = 'ja-JP'
    const label = providerDisplayNameLabel(t, 'comfyui', 'ComfyUI (self-hosted)')
    expect(label).not.toBe('ComfyUI (self-hosted)')
    expect(label).toBe(nested(jaJP, `${PROVIDER_DISPLAY_NAME_NAMESPACE}.comfyui`))
  })

  it('falls back to the backend brand name for a non-descriptive provider', () => {
    i18n.global.locale.value = 'zh-TW'
    expect(providerDisplayNameLabel(t, 'openai', 'OpenAI')).toBe('OpenAI')
  })

  it('falls back to the display name for an unknown provider id', () => {
    expect(providerDisplayNameLabel(t, 'brand_new_provider', 'Brand New')).toBe('Brand New')
    expect(providerDisplayNameLabel(t, '', 'Whatever')).toBe('Whatever')
  })
})

describe('catalog label i18n parity', () => {
  it.each(LOCALES)('%s translates every feature key chip', (_name, catalog) => {
    for (const key of GLOBAL_FEATURE_KEYS) {
      const value = nested(catalog, `${FEATURE_KEY_LABEL_NAMESPACE}.${key}`)
      expect(typeof value, `missing featureKeys.${key}`).toBe('string')
      expect((value as string).trim().length).toBeGreaterThan(0)
    }
  })

  it.each(LOCALES)('%s translates every image feature key', (_name, catalog) => {
    for (const key of IMAGE_FEATURE_KEYS) {
      const value = nested(catalog, `${IMAGE_FEATURE_KEY_LABEL_NAMESPACE}.${key}`)
      expect(typeof value, `missing imageProfilesPicker.featureKeys.${key}`).toBe('string')
      expect((value as string).trim().length).toBeGreaterThan(0)
    }
  })

  it.each(LOCALES)('%s translates every video feature key', (_name, catalog) => {
    for (const key of VIDEO_FEATURE_KEYS) {
      const value = nested(catalog, `${VIDEO_FEATURE_KEY_LABEL_NAMESPACE}.${key}`)
      expect(typeof value, `missing videoProfilesPicker.featureKeys.${key}`).toBe('string')
      expect((value as string).trim().length).toBeGreaterThan(0)
    }
  })

  it.each(LOCALES)('%s translates every provider field label', (_name, catalog) => {
    for (const key of PROVIDER_FIELD_KEYS) {
      const value = nested(catalog, `${PROVIDER_FIELD_NAMESPACE}.${key}.label`)
      expect(typeof value, `missing providerFields.${key}.label`).toBe('string')
      expect((value as string).trim().length).toBeGreaterThan(0)
    }
  })

  it.each(LOCALES)('%s translates every provider field placeholder', (_name, catalog) => {
    for (const key of PROVIDER_FIELD_KEYS_WITH_PLACEHOLDER) {
      const value = nested(catalog, `${PROVIDER_FIELD_NAMESPACE}.${key}.placeholder`)
      expect(typeof value, `missing providerFields.${key}.placeholder`).toBe('string')
      expect((value as string).trim().length).toBeGreaterThan(0)
    }
  })

  it.each(LOCALES)('%s translates every provider field hint', (_name, catalog) => {
    for (const key of PROVIDER_FIELD_KEYS_WITH_HINT) {
      const value = nested(catalog, `${PROVIDER_FIELD_NAMESPACE}.${key}.hint`)
      expect(typeof value, `missing providerFields.${key}.hint`).toBe('string')
      expect((value as string).trim().length).toBeGreaterThan(0)
    }
  })
})
