import { describe, expect, it } from 'vitest'

import {
  resolveStageAccessNotice,
  shouldOpenStageAccessNotice,
} from '@/utils/stageAccessNotice'

describe('shouldOpenStageAccessNotice', () => {
  it('always opens for explicit player actions (Stage tab click, retry)', () => {
    // Refusing an explicit Stage attempt without explanation would strand
    // the player away from the phone/meet/retry affordances.
    expect(shouldOpenStageAccessNotice('explicit', true)).toBe(true)
    expect(shouldOpenStageAccessNotice('explicit', false)).toBe(true)
  })

  it('respects the preference for ambient verdicts', () => {
    // Hint ON = shipped behavior: the collapsed strip appears when a
    // background verdict resolves to warn/block on chat open.
    expect(shouldOpenStageAccessNotice('ambient', true)).toBe(true)
    // Hint OFF = the player opted out of unsolicited banners.
    expect(shouldOpenStageAccessNotice('ambient', false)).toBe(false)
  })
})

describe('resolveStageAccessNotice', () => {
  it('hides the notice for allow decisions and missing verdicts', () => {
    expect(
      resolveStageAccessNotice({ noticeOpen: true, decision: 'allow', expanded: false }),
    ).toEqual({ visible: false, collapsed: false, showDetails: false })
    expect(
      resolveStageAccessNotice({ noticeOpen: true, decision: null, expanded: true }),
    ).toEqual({ visible: false, collapsed: false, showDetails: false })
  })

  it('stays hidden when the notice is not armed, regardless of verdict', () => {
    expect(
      resolveStageAccessNotice({ noticeOpen: false, decision: 'block', expanded: true }),
    ).toEqual({ visible: false, collapsed: false, showDetails: false })
  })

  it('renders collapsed with an expand affordance by default', () => {
    expect(
      resolveStageAccessNotice({ noticeOpen: true, decision: 'block', expanded: false }),
    ).toEqual({ visible: true, collapsed: true, showDetails: false })
  })

  it('shows the phone/meet/retry details once expanded', () => {
    expect(
      resolveStageAccessNotice({ noticeOpen: true, decision: 'warn', expanded: true }),
    ).toEqual({ visible: true, collapsed: false, showDetails: true })
  })
})
