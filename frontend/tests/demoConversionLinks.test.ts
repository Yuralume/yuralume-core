import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  demoConversionLinks,
  demoRetryActions,
  demoUnavailableActions,
} from '@/utils/demoConversionLinks'

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('demoConversionLinks', () => {
  it('uses landing anchors and Discord defaults', () => {
    expect(demoConversionLinks()).toEqual({
      tier0Url: '/#demo-showcase',
      waitlistUrl: '/#alpha',
      discordUrl: 'https://discord.gg/tF8zw7S6',
      selfHostUrl: '/#tiers',
    })
  })

  it('allows deployment-specific conversion links through Vite env', () => {
    vi.stubEnv('VITE_YURALUME_DEMO_TIER0_URL', 'https://demo.example/#play')
    vi.stubEnv('VITE_YURALUME_DEMO_WAITLIST_URL', 'https://forms.example/waitlist')
    vi.stubEnv('VITE_YURALUME_DEMO_DISCORD_URL', 'https://discord.gg/custom')
    vi.stubEnv('VITE_YURALUME_DEMO_SELF_HOST_URL', 'https://docs.example/self-host')

    expect(demoConversionLinks()).toEqual({
      tier0Url: 'https://demo.example/#play',
      waitlistUrl: 'https://forms.example/waitlist',
      discordUrl: 'https://discord.gg/custom',
      selfHostUrl: 'https://docs.example/self-host',
    })
  })

  it('marks external actions for safe new-tab rendering', () => {
    const retryActions = demoRetryActions()
    const unavailableActions = demoUnavailableActions()

    expect(retryActions.find((action) => action.label === 'Join Discord')).toMatchObject({
      external: true,
      variant: 'secondary',
    })
    expect(unavailableActions.find((action) => action.label === 'Self-host path')).toMatchObject({
      external: false,
      variant: 'secondary',
    })
  })
})
