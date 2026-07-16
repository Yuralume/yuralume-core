import { beforeEach, describe, expect, it, vi } from 'vitest'
import axios from 'axios'

import {
  DemoSessionLoginError,
  getAuthConfig,
  getDemoOAuthConfig,
  loginWithCloudSession,
  loginWithDemoSession,
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
})

describe('getDemoOAuthConfig', () => {
  it('fetches runtime client ids and keeps only non-empty ones', async () => {
    mockedAxios.get.mockResolvedValueOnce({
      data: {
        providers: {
          discord: { client_id: 'disc-123' },
          google: { client_id: '' },
        },
      },
    })

    await expect(getDemoOAuthConfig()).resolves.toEqual({ discord: 'disc-123' })
    expect(mockedAxios.get).toHaveBeenCalledWith('/api/v1/auth/demo/oauth/config')
  })

  it('returns an empty map when no providers are configured', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: {} })

    await expect(getDemoOAuthConfig()).resolves.toEqual({})
  })
})

describe('loginWithDemoSession', () => {
  it('posts OAuth callback material to the Core demo session endpoint', async () => {
    const response = {
      token: 'core-token',
      user: {
        id: 'cloud:demo',
        display_name: 'Demo',
        email: 'demo@example.com',
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

    await expect(loginWithDemoSession({
      provider: 'discord',
      authorization_code: 'oauth-code',
      redirect_uri: 'https://app.example/demo/oauth/discord/callback',
      code_verifier: 'pkce',
    })).resolves.toEqual(response)

    expect(mockedAxios.post).toHaveBeenCalledWith('/api/v1/auth/demo/session', {
      provider: 'discord',
      authorization_code: 'oauth-code',
      redirect_uri: 'https://app.example/demo/oauth/discord/callback',
      code_verifier: 'pkce',
    })
  })

  it('preserves structured demo limit errors from Core', async () => {
    mockedAxios.post.mockRejectedValueOnce({
      response: {
        status: 429,
        data: {
          detail: {
            error: {
              code: 'demo_rate_limited',
              message: 'demo session provisioning is rate limited',
              retryable: true,
            },
          },
        },
      },
    })

    const rejected = loginWithDemoSession({
      provider: 'discord',
      authorization_code: 'oauth-code',
    })

    await expect(rejected).rejects.toBeInstanceOf(DemoSessionLoginError)
    await expect(rejected).rejects.toMatchObject({
      code: 'demo_rate_limited',
      statusCode: 429,
      retryable: true,
    })
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
