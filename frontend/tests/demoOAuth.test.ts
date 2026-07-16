import { describe, expect, it } from 'vitest'

import {
  buildDemoOAuthAuthorizeUrl,
  type DemoOAuthCrypto,
  supportedDemoOAuthProvider,
} from '@/utils/demoOAuth'

describe('supportedDemoOAuthProvider', () => {
  it('normalizes supported demo OAuth providers', () => {
    expect(supportedDemoOAuthProvider(' Discord ')).toBe('discord')
    expect(supportedDemoOAuthProvider('GOOGLE')).toBe('google')
  })

  it('rejects unsupported providers', () => {
    expect(supportedDemoOAuthProvider('github')).toBeNull()
    expect(supportedDemoOAuthProvider('')).toBeNull()
  })
})

describe('buildDemoOAuthAuthorizeUrl', () => {
  it('builds a Discord authorization URL with PKCE verifier storage', async () => {
    const storage = new Map<string, string>()

    const result = await buildDemoOAuthAuthorizeUrl('discord', {
      clientIds: { discord: 'discord-client' },
      cryptoProvider: fakeCrypto(),
      origin: 'https://app.example',
      storage: storageAdapter(storage),
    })

    const url = new URL(result)
    expect(`${url.origin}${url.pathname}`).toBe(
      'https://discord.com/oauth2/authorize',
    )
    expect(url.searchParams.get('client_id')).toBe('discord-client')
    expect(url.searchParams.get('redirect_uri')).toBe(
      'https://app.example/demo/oauth/discord/callback',
    )
    expect(url.searchParams.get('response_type')).toBe('code')
    expect(url.searchParams.get('scope')).toBe('identify email')
    expect(url.searchParams.get('code_challenge_method')).toBe('S256')
    expect(url.searchParams.get('code_challenge')).toBeTruthy()

    const state = url.searchParams.get('state')
    expect(state).toBeTruthy()
    expect(storage.get(`yuralume_demo_oauth_verifier:${state}`)).toBeTruthy()
  })

  it('builds a Google authorization URL with the OpenID scopes', async () => {
    const result = await buildDemoOAuthAuthorizeUrl('google', {
      clientIds: { google: 'google-client' },
      cryptoProvider: fakeCrypto(),
      origin: 'https://app.example',
      storage: storageAdapter(new Map()),
    })

    const url = new URL(result)
    expect(`${url.origin}${url.pathname}`).toBe(
      'https://accounts.google.com/o/oauth2/v2/auth',
    )
    expect(url.searchParams.get('client_id')).toBe('google-client')
    expect(url.searchParams.get('redirect_uri')).toBe(
      'https://app.example/demo/oauth/google/callback',
    )
    expect(url.searchParams.get('scope')).toBe('openid email profile')
    expect(url.searchParams.get('code_challenge_method')).toBe('S256')
  })

  it('fails fast when a provider client id is missing', async () => {
    await expect(
      buildDemoOAuthAuthorizeUrl('discord', {
        clientIds: { discord: '' },
        cryptoProvider: fakeCrypto(),
        origin: 'https://app.example',
        storage: storageAdapter(new Map()),
      }),
    ).rejects.toThrow('discord demo OAuth client id is not configured')
  })
})

function storageAdapter(storage: Map<string, string>) {
  return {
    setItem(key: string, value: string) {
      storage.set(key, value)
    },
  }
}

function fakeCrypto(): DemoOAuthCrypto {
  return {
    getRandomValues(bytes: Uint8Array) {
      bytes.forEach((_, index) => {
        bytes[index] = (index % 251) + 1
      })
      return bytes
    },
    subtle: {
      async digest() {
        const bytes = new Uint8Array(32)
        bytes.fill(7)
        return bytes.buffer
      },
    },
  }
}
