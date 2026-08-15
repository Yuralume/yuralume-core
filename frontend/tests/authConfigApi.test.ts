import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'

import {
  getAuthConfig,
  loginWithCloudSession,
} from '@/utils/api/auth'

vi.mock('axios', () => {
  const api = {
    get: vi.fn(),
    post: vi.fn(),
  }
  return { default: api }
})

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
})

describe('getAuthConfig', () => {
  it('loads auth config with build info from the startup endpoint', async () => {
    const response = {
      auth_enabled: true,
      needs_setup: false,
      mode: 'self_host',
      debug_ui_enabled: false,
      build_info: {
        name: 'Yuralume Core',
        version: '0.1.0',
        api_version: 'v1',
        build: {
          image_tag: 'v0.1.0',
          commit_sha: 'abcdef123456',
          built_at: '2026-06-14T12:00:00Z',
        },
      },
    }
    mockedAxios.get.mockResolvedValueOnce({ data: response })

    await expect(getAuthConfig()).resolves.toEqual(response)

    expect(mockedAxios.get).toHaveBeenCalledWith('/api/v1/auth/config')
  })

  it('carries the hosted portal url when the deployment advertises one', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: {
        auth_enabled: true,
        needs_setup: false,
        mode: 'cloud',
        portal_url: 'https://app.yuralume.com',
      },
    })

    await expect(getAuthConfig()).resolves.toMatchObject({
      portal_url: 'https://app.yuralume.com',
    })
  })

  it('leaves portal_url absent for self-host', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: { auth_enabled: true, needs_setup: false, mode: 'self_host' },
    })

    const config = await getAuthConfig()

    expect(config.portal_url ?? null).toBeNull()
  })
})

describe('loginWithCloudSession', () => {
  it('posts the one-time hosted-play code to the Core cloud session endpoint', async () => {
    const response = {
      token: 'core-token',
      user: {
        id: 'cloud:acct-hosted',
        display_name: 'Hosted Player',
        email: 'player@example.com',
        is_admin: false,
        primary_language: 'en-US',
        timezone_id: 'UTC',
        country_code: null,
        latitude: null,
        longitude: null,
        location_label: null,
      },
    }
    mockedAxios.post.mockResolvedValueOnce({ data: response })

    await expect(
      loginWithCloudSession({ code: 'yhp_entry' }),
    ).resolves.toEqual(response)

    expect(mockedAxios.post).toHaveBeenCalledWith('/api/v1/auth/cloud/session', {
      code: 'yhp_entry',
    })
  })
})
