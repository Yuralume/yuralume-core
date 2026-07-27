import { DemoSessionLoginError } from '@/utils/api/auth'
import {
  demoRetryActions,
  demoUnavailableActions,
  type DemoConversionAction,
} from '@/utils/demoConversionLinks'

/**
 * Classification only — the copy itself lives in the trilingual catalog
 * under `demo.errors.*`, so the demo pages resolve it through `t()` like
 * every other surface (plan U1-D).
 */
export interface DemoSessionErrorCopy {
  titleKey: string
  messageKey: string
  actions: DemoConversionAction[]
}

export function demoSessionErrorCopy(error: unknown): DemoSessionErrorCopy {
  if (error instanceof DemoSessionLoginError) {
    if (error.code === 'demo_busy') {
      return {
        titleKey: 'demo.errors.busyTitle',
        messageKey: 'demo.errors.busyBody',
        actions: demoRetryActions(),
      }
    }
    if (error.code === 'demo_rate_limited') {
      return {
        titleKey: 'demo.errors.rateLimitedTitle',
        messageKey: 'demo.errors.rateLimitedBody',
        actions: demoUnavailableActions(),
      }
    }
  }
  return {
    titleKey: 'demo.errors.unavailableTitle',
    messageKey: 'demo.errors.unavailableBody',
    actions: demoUnavailableActions(),
  }
}
