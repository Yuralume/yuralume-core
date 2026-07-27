import { describe, expect, it, vi } from 'vitest'

import {
  isTTSPlaybackEligible,
  resolveTTSAvailability,
} from '@/utils/ttsAvailability'
import type { TTSAssetCatalog } from '@/utils/api/ttsAssets'

function catalog(
  enabled: boolean,
  completeVoices: boolean[],
): TTSAssetCatalog {
  return {
    enabled,
    install_dir: null,
    ref_audios: [],
    gpt_weights: [],
    sovits_weights: [],
    voice_presets: completeVoices.map((isComplete, index) => ({
      id: `voice-${index}`,
      label: `Voice ${index}`,
      voice_id: `voice-${index}`,
      prompt_lang: 'ja',
      is_complete: isComplete,
    })),
  }
}

describe('resolveTTSAvailability', () => {
  it('只在通道啟用且至少有一個完整 voice 時回 true', async () => {
    await expect(resolveTTSAvailability(() => Promise.resolve(catalog(true, [true]))))
      .resolves.toBe(true)
    await expect(resolveTTSAvailability(() => Promise.resolve(catalog(false, [true]))))
      .resolves.toBe(false)
    await expect(resolveTTSAvailability(() => Promise.resolve(catalog(true, [false]))))
      .resolves.toBe(false)
    await expect(resolveTTSAvailability(() => Promise.resolve(catalog(true, []))))
      .resolves.toBe(false)
  })

  it('探測失敗時 fail-closed，不顯示無法使用的入口', async () => {
    const loadCatalog = vi.fn().mockRejectedValue(new Error('offline'))

    await expect(resolveTTSAvailability(loadCatalog)).resolves.toBe(false)
  })
})

describe('isTTSPlaybackEligible', () => {
  const eligibleMessage = {
    channelAvailable: true,
    isAssistantMessage: true,
    characterId: 'character-1',
    speechText: '晚安。',
  }

  it('有效通道上的角色回覆才顯示播放入口', () => {
    expect(isTTSPlaybackEligible(eligibleMessage)).toBe(true)
  })

  it('沒有有效通道時不顯示播放入口', () => {
    expect(isTTSPlaybackEligible({
      ...eligibleMessage,
      channelAvailable: false,
    })).toBe(false)
  })

  it('非角色回覆或沒有可朗讀文字時不顯示播放入口', () => {
    expect(isTTSPlaybackEligible({
      ...eligibleMessage,
      isAssistantMessage: false,
    })).toBe(false)
    expect(isTTSPlaybackEligible({
      ...eligibleMessage,
      characterId: null,
    })).toBe(false)
    expect(isTTSPlaybackEligible({
      ...eligibleMessage,
      speechText: '',
    })).toBe(false)
  })
})
