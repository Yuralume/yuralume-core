import { describe, expect, it } from 'vitest'
import { composerHeightFor, type ComposerMetrics } from '@/utils/composerAutoResize'

/** 44px 單行 textarea：padding 10+10、border 1+1、line-height 22.4。 */
function metrics(partial: Partial<ComposerMetrics> = {}): ComposerMetrics {
  return {
    value: '',
    scrollHeight: 42,
    offsetHeight: 44,
    clientHeight: 42,
    ...partial,
  }
}

describe('composerHeightFor', () => {
  it('空輸入交還給 CSS，即使 Blink 把 placeholder 算進 scrollHeight', () => {
    // 360px 視窗下 Blink 實測值：placeholder 折成五行 => 154px。
    expect(composerHeightFor(metrics({ value: '', scrollHeight: 154 }))).toBeNull()
  })

  it('極窄螢幕的 placeholder 也不會撐高（280px 實測 602px）', () => {
    expect(composerHeightFor(metrics({ value: '', scrollHeight: 602 }))).toBeNull()
  })

  it('Gecko 的空值路徑同樣交還給 CSS', () => {
    expect(composerHeightFor(metrics({ value: '', scrollHeight: 42 }))).toBeNull()
  })

  it('有內容時長到 scrollHeight，並補回 border-box 少掉的邊框', () => {
    expect(composerHeightFor(metrics({ value: '嗨', scrollHeight: 42 }))).toBe('44px')
  })

  it('多行內容一樣補邊框', () => {
    expect(composerHeightFor(metrics({ value: 'a\nb\nc', scrollHeight: 109 }))).toBe('111px')
  })

  it('只有空白字元仍算有內容——玩家可能正在打字中間', () => {
    expect(composerHeightFor(metrics({ value: ' ', scrollHeight: 42 }))).toBe('44px')
  })

  it('量不到邊框時不會產生負值', () => {
    expect(
      composerHeightFor(metrics({ value: '嗨', scrollHeight: 42, offsetHeight: 0, clientHeight: 42 })),
    ).toBe('42px')
  })
})
