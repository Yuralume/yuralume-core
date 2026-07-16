import { describe, expect, it } from 'vitest'
import { shouldSendChatInputOnKeydown } from '@/utils/chatInputKeys'

type TestKeyboardEventInit = KeyboardEventInit & {
  keyCode?: number
  which?: number
}

function keydown(init: TestKeyboardEventInit): KeyboardEvent {
  return init as KeyboardEvent
}

describe('shouldSendChatInputOnKeydown', () => {
  it('sends on plain Enter', () => {
    expect(shouldSendChatInputOnKeydown(keydown({ key: 'Enter' }))).toBe(true)
  })

  it('does not send on Shift+Enter', () => {
    expect(shouldSendChatInputOnKeydown(
      keydown({ key: 'Enter', shiftKey: true }),
    )).toBe(false)
  })

  it('does not send while the IME composition state is active', () => {
    expect(shouldSendChatInputOnKeydown(
      keydown({ key: 'Enter' }),
      true,
    )).toBe(false)
  })

  it('does not send when the native event is composing', () => {
    expect(shouldSendChatInputOnKeydown(
      keydown({ key: 'Enter', isComposing: true }),
    )).toBe(false)
  })

  it('does not send for legacy IME keydown code 229', () => {
    expect(shouldSendChatInputOnKeydown(
      keydown({ key: 'Enter', keyCode: 229, which: 229 }),
    )).toBe(false)
  })
})
