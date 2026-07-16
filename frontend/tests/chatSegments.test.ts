import { describe, expect, it } from 'vitest'
import {
  revealDelaysFor,
  revealDelayMs,
  splitAssistantBubbles,
  stripActionNarration,
  TEXTING_REVEAL_DELAY,
} from '@/utils/chatSegments'

describe('splitAssistantBubbles', () => {
  it('splits plain assistant text on blank-line message boundaries', () => {
    expect(splitAssistantBubbles('欸\n\n我剛想到一件事\n\n你等等有空嗎')).toEqual([
      '欸',
      '我剛想到一件事',
      '你等等有空嗎',
    ])
  })

  it('keeps stage action narration in one bubble', () => {
    expect(splitAssistantBubbles('*抬頭看你*\n\n你來啦。')).toEqual([
      '*抬頭看你*\n\n你來啦。',
    ])
  })

  it('strips action narration and still splits in text-message mode', () => {
    expect(splitAssistantBubbles(
      '真的好久沒聯絡耶！\n\n*把手機相簿往下滑* 我最近在整理一些照片\n\n你要看嗎？',
      { stripActionNarration: true },
    )).toEqual([
      '真的好久沒聯絡耶！',
      '我最近在整理一些照片',
      '你要看嗎？',
    ])
  })

  it('trims surrounding whitespace and removes empty runs', () => {
    expect(splitAssistantBubbles('\n\n  第一則  \n\n\n  第二則\n\n')).toEqual([
      '第一則',
      '第二則',
    ])
  })

  it('returns a single segment for one-message replies', () => {
    expect(splitAssistantBubbles('我在，怎麼了？')).toEqual(['我在，怎麼了？'])
  })

  it('returns no segments for blank content', () => {
    expect(splitAssistantBubbles(' \n\n ')).toEqual([])
  })
})

describe('stripActionNarration', () => {
  it('removes single-line star action narration while preserving speech', () => {
    expect(stripActionNarration('*把手機相簿往下滑* 我最近在整理照片')).toBe(
      '我最近在整理照片',
    )
  })
})

describe('revealDelayMs', () => {
  it('uses the configured base delay and clamps the upper bound', () => {
    expect(revealDelayMs('')).toBe(TEXTING_REVEAL_DELAY.baseMs)
    expect(revealDelayMs('x'.repeat(500))).toBe(TEXTING_REVEAL_DELAY.maxMs)
  })
})

describe('revealDelaysFor', () => {
  it('returns one inter-bubble delay per hidden follow-up bubble without compression', () => {
    const segments = ['短短第一則', '第二則也不長', '最後一則']

    expect(revealDelaysFor(segments)).toEqual([
      revealDelayMs(segments[0]),
      revealDelayMs(segments[1]),
    ])
  })

  it('does not compress delays when the total exactly matches the cap', () => {
    const segments = [
      'x'.repeat(50),
      'x'.repeat(50),
      'x'.repeat(50),
      'x'.repeat(50),
      '最後一則',
    ]

    const delays = revealDelaysFor(segments)

    expect(delays).toEqual([1250, 1250, 1250, 1250])
    expect(delays.reduce((total, delay) => total + delay, 0)).toBe(
      TEXTING_REVEAL_DELAY.totalCapMs,
    )
  })

  it('compresses long multi-bubble reveals under the total cap', () => {
    const segments = Array.from({ length: 6 }, () => 'x'.repeat(500))

    const delays = revealDelaysFor(segments)

    expect(delays).toHaveLength(5)
    expect(delays.reduce((total, delay) => total + delay, 0)).toBeLessThanOrEqual(
      TEXTING_REVEAL_DELAY.totalCapMs,
    )
    expect(delays.every(delay => delay >= TEXTING_REVEAL_DELAY.minMs)).toBe(true)
  })

  it('keeps every delay at the minimum when too many bubbles cannot fit the cap', () => {
    const segments = Array.from({ length: 30 }, (_, index) => `第 ${index + 1} 則`)

    const delays = revealDelaysFor(segments)

    expect(delays).toHaveLength(29)
    expect(delays.every(delay => delay === TEXTING_REVEAL_DELAY.minMs)).toBe(true)
  })

  it('returns no delays for empty or single-bubble replies', () => {
    expect(revealDelaysFor([])).toEqual([])
    expect(revealDelaysFor(['只有一則'])).toEqual([])
  })
})
