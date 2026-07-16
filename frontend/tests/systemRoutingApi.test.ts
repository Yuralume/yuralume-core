import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  getActiveImageProfilePreference,
  getActiveModelPreference,
  getActiveVideoProfilePreference,
  getFeatureModelGroups,
  getFeatureModelPreferences,
  getImageFeatureProfilePreferences,
  getVideoFeatureProfilePreferences,
  setActiveImageProfilePreference,
  setActiveModelPreference,
  setActiveVideoProfilePreference,
  setFeatureModelPreferences,
  setImageFeatureProfilePreferences,
  setVideoFeatureProfilePreferences,
  updateFeatureModelGroups,
  type FeatureModelGroupsPreference,
  type FeatureModelsPreference,
  type ImageFeatureProfilesPreference,
  type VideoFeatureProfilesPreference,
} from '@/utils/api/system'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    put: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

const globalScope = {
  params: { scope: 'global' },
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('system routing API scope', () => {
  it('uses installation-wide scope for LLM routing preferences by default', async () => {
    const active = { provider_id: 'openai', model_id: 'gpt-5.4-mini' }
    const features: FeatureModelsPreference = {
      overrides: {},
      known_keys: ['chat'],
      labels: { chat: 'Chat' },
    }
    const groups: FeatureModelGroupsPreference = {
      active_model: active,
      groups: [],
    }
    mockedAxios.get
      .mockResolvedValueOnce({ data: active })
      .mockResolvedValueOnce({ data: features })
      .mockResolvedValueOnce({ data: groups })
    mockedAxios.put
      .mockResolvedValueOnce({ data: active })
      .mockResolvedValueOnce({ data: features })
      .mockResolvedValueOnce({ data: groups })

    await expect(getActiveModelPreference()).resolves.toEqual(active)
    await expect(getFeatureModelPreferences()).resolves.toEqual(features)
    await expect(getFeatureModelGroups()).resolves.toEqual(groups)
    await expect(setActiveModelPreference(active)).resolves.toEqual(active)
    await expect(setFeatureModelPreferences(features)).resolves.toEqual(features)
    await expect(updateFeatureModelGroups({ feature_model_groups: {} }))
      .resolves.toEqual(groups)

    expect(mockedAxios.get).toHaveBeenNthCalledWith(
      1,
      '/api/v1/system/preferences/active-model',
      globalScope,
    )
    expect(mockedAxios.get).toHaveBeenNthCalledWith(
      2,
      '/api/v1/system/preferences/feature-models',
      globalScope,
    )
    expect(mockedAxios.get).toHaveBeenNthCalledWith(
      3,
      '/api/v1/system/preferences/feature-model-groups',
      globalScope,
    )
    expect(mockedAxios.put).toHaveBeenNthCalledWith(
      1,
      '/api/v1/system/preferences/active-model',
      active,
      globalScope,
    )
    expect(mockedAxios.put).toHaveBeenNthCalledWith(
      2,
      '/api/v1/system/preferences/feature-models',
      features,
      globalScope,
    )
    expect(mockedAxios.put).toHaveBeenNthCalledWith(
      3,
      '/api/v1/system/preferences/feature-model-groups',
      { feature_model_groups: {} },
      globalScope,
    )
  })

  it('uses installation-wide scope for media routing preferences by default', async () => {
    const activeImage = { profile_id: 'openai-image' }
    const imageFeatures: ImageFeatureProfilesPreference = {
      overrides: {},
      known_keys: ['image_feed'],
      labels: { image_feed: 'Feed image' },
    }
    const activeVideo = { profile_id: 'veo' }
    const videoFeatures: VideoFeatureProfilesPreference = {
      overrides: {},
      known_keys: ['video_feed'],
      labels: { video_feed: 'Feed video' },
    }
    mockedAxios.get
      .mockResolvedValueOnce({ data: activeImage })
      .mockResolvedValueOnce({ data: imageFeatures })
      .mockResolvedValueOnce({ data: activeVideo })
      .mockResolvedValueOnce({ data: videoFeatures })
    mockedAxios.put
      .mockResolvedValueOnce({ data: activeImage })
      .mockResolvedValueOnce({ data: imageFeatures })
      .mockResolvedValueOnce({ data: activeVideo })
      .mockResolvedValueOnce({ data: videoFeatures })

    await expect(getActiveImageProfilePreference()).resolves.toEqual(activeImage)
    await expect(getImageFeatureProfilePreferences()).resolves.toEqual(imageFeatures)
    await expect(getActiveVideoProfilePreference()).resolves.toEqual(activeVideo)
    await expect(getVideoFeatureProfilePreferences()).resolves.toEqual(videoFeatures)
    await expect(setActiveImageProfilePreference(activeImage)).resolves.toEqual(activeImage)
    await expect(setImageFeatureProfilePreferences(imageFeatures)).resolves.toEqual(imageFeatures)
    await expect(setActiveVideoProfilePreference(activeVideo)).resolves.toEqual(activeVideo)
    await expect(setVideoFeatureProfilePreferences(videoFeatures)).resolves.toEqual(videoFeatures)

    expect(mockedAxios.get).toHaveBeenNthCalledWith(
      1,
      '/api/v1/system/preferences/active-image-profile',
      globalScope,
    )
    expect(mockedAxios.get).toHaveBeenNthCalledWith(
      2,
      '/api/v1/system/preferences/image-feature-profiles',
      globalScope,
    )
    expect(mockedAxios.get).toHaveBeenNthCalledWith(
      3,
      '/api/v1/system/preferences/active-video-profile',
      globalScope,
    )
    expect(mockedAxios.get).toHaveBeenNthCalledWith(
      4,
      '/api/v1/system/preferences/video-feature-profiles',
      globalScope,
    )
    expect(mockedAxios.put).toHaveBeenNthCalledWith(
      1,
      '/api/v1/system/preferences/active-image-profile',
      activeImage,
      globalScope,
    )
    expect(mockedAxios.put).toHaveBeenNthCalledWith(
      2,
      '/api/v1/system/preferences/image-feature-profiles',
      imageFeatures,
      globalScope,
    )
    expect(mockedAxios.put).toHaveBeenNthCalledWith(
      3,
      '/api/v1/system/preferences/active-video-profile',
      activeVideo,
      globalScope,
    )
    expect(mockedAxios.put).toHaveBeenNthCalledWith(
      4,
      '/api/v1/system/preferences/video-feature-profiles',
      videoFeatures,
      globalScope,
    )
  })

  it('still allows explicit user scope for personal callers', async () => {
    const active = { provider_id: 'openai', model_id: 'gpt-4o' }
    mockedAxios.get.mockResolvedValueOnce({ data: active })

    await expect(getActiveModelPreference('user')).resolves.toEqual(active)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/system/preferences/active-model',
      { params: { scope: 'user' } },
    )
  })
})
