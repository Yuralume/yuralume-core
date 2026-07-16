import { describe, expect, it } from 'vitest'

import {
  CHAT_ASSIST_DISCOVERED_KEY,
  CHAT_ASSIST_HINT_DISMISSED_KEY,
  isChatAssistDiscovered,
  isChatAssistHintDismissed,
  rememberChatAssistDiscovered,
  rememberChatAssistHintDismissed,
  shouldShowChatAssistHint,
} from '@/utils/chatAssistDiscovery'

function fakeStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

describe('shouldShowChatAssistHint', () => {
  it('shows after a chat has started when chat assist is enabled and the input is empty', () => {
    expect(shouldShowChatAssistHint({
      enabled: true,
      assistOpen: false,
      hasMessages: true,
      inputEmpty: true,
      discovered: false,
      dismissed: false,
    })).toBe(true)
  })

  it('hides when chat assist is disabled', () => {
    expect(shouldShowChatAssistHint({
      enabled: false,
      assistOpen: false,
      hasMessages: true,
      inputEmpty: true,
      discovered: false,
      dismissed: false,
    })).toBe(false)
  })

  it('hides before the first turn', () => {
    expect(shouldShowChatAssistHint({
      enabled: true,
      assistOpen: false,
      hasMessages: false,
      inputEmpty: true,
      discovered: false,
      dismissed: false,
    })).toBe(false)
  })

  it('hides while the player is typing', () => {
    expect(shouldShowChatAssistHint({
      enabled: true,
      assistOpen: false,
      hasMessages: true,
      inputEmpty: false,
      discovered: false,
      dismissed: false,
    })).toBe(false)
  })

  it('hides while the assist panel is open', () => {
    expect(shouldShowChatAssistHint({
      enabled: true,
      assistOpen: true,
      hasMessages: true,
      inputEmpty: true,
      discovered: false,
      dismissed: false,
    })).toBe(false)
  })

  it('hides after discovery or manual dismissal', () => {
    const base = {
      enabled: true,
      assistOpen: false,
      hasMessages: true,
      inputEmpty: true,
    }

    expect(shouldShowChatAssistHint({
      ...base,
      discovered: true,
      dismissed: false,
    })).toBe(false)
    expect(shouldShowChatAssistHint({
      ...base,
      discovered: false,
      dismissed: true,
    })).toBe(false)
  })
})

describe('chat assist discovery storage', () => {
  it('stores user-wide discovery and dismissal flags', () => {
    const storage = fakeStorage()

    expect(isChatAssistDiscovered(storage)).toBe(false)
    expect(rememberChatAssistDiscovered(storage)).toBe(true)
    expect(isChatAssistDiscovered(storage)).toBe(true)
    expect(storage.getItem(CHAT_ASSIST_DISCOVERED_KEY)).toBe('1')

    expect(isChatAssistHintDismissed(storage)).toBe(false)
    expect(rememberChatAssistHintDismissed(storage)).toBe(true)
    expect(isChatAssistHintDismissed(storage)).toBe(true)
    expect(storage.getItem(CHAT_ASSIST_HINT_DISMISSED_KEY)).toBe('1')
  })

  it('fails soft when localStorage is unavailable or throws', () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    } satisfies Pick<Storage, 'getItem' | 'setItem'>

    expect(isChatAssistDiscovered(null)).toBe(false)
    expect(rememberChatAssistDiscovered(null)).toBe(false)
    expect(isChatAssistHintDismissed(null)).toBe(false)
    expect(rememberChatAssistHintDismissed(null)).toBe(false)
    expect(isChatAssistDiscovered(throwingStorage)).toBe(false)
    expect(rememberChatAssistDiscovered(throwingStorage)).toBe(false)
    expect(isChatAssistHintDismissed(throwingStorage)).toBe(false)
    expect(rememberChatAssistHintDismissed(throwingStorage)).toBe(false)
  })
})
