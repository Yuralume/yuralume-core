import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'
import {
  getAdminNsfwModeTarget,
  getNsfwModePreference,
  setAdminNsfwModeTarget,
  setNsfwModePreference,
  type NsfwModePreference,
} from '@/utils/api/system'
import { synthesizeCharacterTTS, TTSDisabledError } from '@/utils/api/tts'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    put: vi.fn(),
    post: vi.fn(),
    isAxiosError: vi.fn((error: unknown) => (
      Boolean(error && typeof error === 'object' && 'isAxiosError' in error)
    )),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

function nsfwModePreference(): NsfwModePreference {
  return {
    active: true,
    configured: true,
    locked: false,
    ttl_seconds: 1800,
    last_activity_at: '2026-06-13T01:02:03Z',
    expires_at: '2026-06-13T01:32:03Z',
    target: {
      llm_provider_id: 'local',
      llm_model_id: 'adult-model',
      image_profile_id: 'anime_nsfw',
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('NSFW mode system API', () => {
  it('loads the current NSFW mode preference', async () => {
    const response = nsfwModePreference()
    mockedAxios.get.mockResolvedValueOnce({ data: response })

    await expect(getNsfwModePreference()).resolves.toEqual(response)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/system/preferences/nsfw-mode',
    )
  })

  it('sends only the active flag when enabling NSFW mode', async () => {
    const response = nsfwModePreference()
    const payload = { active: true }
    mockedAxios.put.mockResolvedValueOnce({ data: response })

    await expect(setNsfwModePreference(payload)).resolves.toEqual(response)

    expect(mockedAxios.put).toHaveBeenCalledWith(
      '/api/v1/system/preferences/nsfw-mode',
      payload,
    )
  })

  it('loads and saves the admin routing target separately', async () => {
    const target = nsfwModePreference().target!
    const response = {
      configured: true,
      locked: false,
      target,
    }
    mockedAxios.get.mockResolvedValueOnce({ data: response })
    mockedAxios.put.mockResolvedValueOnce({ data: response })

    await expect(getAdminNsfwModeTarget()).resolves.toEqual(response)
    await expect(setAdminNsfwModeTarget(target)).resolves.toEqual(response)

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/admin/system/preferences/nsfw-mode-target',
    )
    expect(mockedAxios.put).toHaveBeenCalledWith(
      '/api/v1/admin/system/preferences/nsfw-mode-target',
      target,
    )
  })
})

describe('TTS disabled mapping', () => {
  it('maps NSFW-mode 403 responses to TTSDisabledError', async () => {
    mockedAxios.post.mockRejectedValueOnce({
      isAxiosError: true,
      response: {
        status: 403,
        data: { detail: 'TTS is disabled while NSFW mode is active' },
      },
    })

    await expect(
      synthesizeCharacterTTS('char-1', 'hello'),
    ).rejects.toBeInstanceOf(TTSDisabledError)
  })
})
