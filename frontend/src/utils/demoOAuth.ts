export type DemoOAuthProvider = 'discord' | 'google'

interface DemoOAuthProviderConfig {
  authorizeUrl: string
  scope: string
}

type DemoOAuthClientIds = Partial<Record<DemoOAuthProvider, string>>

interface DemoOAuthStorage {
  setItem(key: string, value: string): void
}

export interface DemoOAuthCrypto {
  getRandomValues(bytes: Uint8Array): Uint8Array
  subtle: Pick<SubtleCrypto, 'digest'>
}

export interface DemoOAuthAuthorizeUrlOptions {
  clientIds?: DemoOAuthClientIds
  cryptoProvider?: DemoOAuthCrypto
  origin?: string
  storage?: DemoOAuthStorage
}

const PROVIDERS: Record<DemoOAuthProvider, DemoOAuthProviderConfig> = {
  discord: {
    authorizeUrl: 'https://discord.com/oauth2/authorize',
    scope: 'identify email',
  },
  google: {
    authorizeUrl: 'https://accounts.google.com/o/oauth2/v2/auth',
    scope: 'openid email profile',
  },
}

const DEFAULT_CLIENT_IDS: Record<DemoOAuthProvider, string> = {
  discord: import.meta.env.VITE_YURALUME_DEMO_DISCORD_CLIENT_ID || '',
  google: import.meta.env.VITE_YURALUME_DEMO_GOOGLE_CLIENT_ID || '',
}

export function supportedDemoOAuthProvider(
  raw: string,
): DemoOAuthProvider | null {
  const provider = raw.trim().toLowerCase()
  return provider === 'discord' || provider === 'google' ? provider : null
}

export async function buildDemoOAuthAuthorizeUrl(
  provider: DemoOAuthProvider,
  options: DemoOAuthAuthorizeUrlOptions = {},
): Promise<string> {
  const config = PROVIDERS[provider]
  const clientId = resolveClientId(provider, options.clientIds)
  if (!clientId) {
    throw new Error(`${provider} demo OAuth client id is not configured`)
  }
  const oauthCrypto = options.cryptoProvider ?? requireCrypto()
  const state = randomUrlToken(oauthCrypto)
  const verifier = randomUrlToken(oauthCrypto, 64)
  const challenge = await sha256Base64Url(verifier, oauthCrypto)
  const storage = options.storage ?? requireSessionStorage()
  storage.setItem(`yuralume_demo_oauth_verifier:${state}`, verifier)

  const origin = options.origin ?? requireWindowOrigin()
  const redirectUri = `${origin}/demo/oauth/${provider}/callback`
  const url = new URL(config.authorizeUrl)
  url.searchParams.set('client_id', clientId)
  url.searchParams.set('redirect_uri', redirectUri)
  url.searchParams.set('response_type', 'code')
  url.searchParams.set('scope', config.scope)
  url.searchParams.set('state', state)
  url.searchParams.set('code_challenge', challenge)
  url.searchParams.set('code_challenge_method', 'S256')
  return url.toString()
}

function resolveClientId(
  provider: DemoOAuthProvider,
  clientIds?: DemoOAuthClientIds,
): string {
  if (clientIds && Object.prototype.hasOwnProperty.call(clientIds, provider)) {
    return (clientIds[provider] || '').trim()
  }
  return DEFAULT_CLIENT_IDS[provider].trim()
}

function randomUrlToken(oauthCrypto: DemoOAuthCrypto, byteLength = 32): string {
  const bytes = new Uint8Array(byteLength)
  oauthCrypto.getRandomValues(bytes)
  return base64Url(bytes)
}

async function sha256Base64Url(
  value: string,
  oauthCrypto: DemoOAuthCrypto,
): Promise<string> {
  const bytes = new TextEncoder().encode(value)
  const digest = await oauthCrypto.subtle.digest('SHA-256', bytes)
  return base64Url(new Uint8Array(digest))
}

function base64Url(bytes: Uint8Array): string {
  let binary = ''
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte)
  })
  return btoa(binary)
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '')
}

function requireWindowOrigin(): string {
  if (typeof window === 'undefined') {
    throw new Error('demo OAuth requires a browser window origin')
  }
  return window.location.origin
}

function requireSessionStorage(): DemoOAuthStorage {
  if (typeof sessionStorage === 'undefined') {
    throw new Error('demo OAuth requires sessionStorage')
  }
  return sessionStorage
}

function requireCrypto(): DemoOAuthCrypto {
  if (typeof crypto === 'undefined' || !crypto.subtle) {
    throw new Error('demo OAuth requires browser crypto support')
  }
  return crypto
}
