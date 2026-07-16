import { DemoSessionLoginError } from '@/utils/api/auth'
import {
  demoRetryActions,
  demoUnavailableActions,
  type DemoConversionAction,
} from '@/utils/demoConversionLinks'

export interface DemoSessionErrorCopy {
  title: string
  message: string
  actions: DemoConversionAction[]
}

export function demoSessionErrorCopy(error: unknown): DemoSessionErrorCopy {
  if (error instanceof DemoSessionLoginError) {
    if (error.code === 'demo_busy') {
      return {
        title: 'Demo is full',
        message: 'All public demo seats are in use. Try Tier 0 now, join the waitlist, or follow Discord for the next live-demo window.',
        actions: demoRetryActions(),
      }
    }
    if (error.code === 'demo_rate_limited') {
      return {
        title: 'Demo limit reached',
        message: 'This browser or network has reached the demo start limit. Try Tier 0, join Discord, or use the self-host path while the public demo resets.',
        actions: demoUnavailableActions(),
      }
    }
  }
  return {
    title: 'Demo unavailable',
    message: 'The demo session could not be started. Try the scripted character, join Discord for status, or continue through self-host.',
    actions: demoUnavailableActions(),
  }
}
