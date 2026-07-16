import { describe, expect, it } from 'vitest'

import {
  MODEL_ROUTING_DISCOVERED_KEY,
  isModelRoutingDiscovered,
  rememberModelRoutingDiscovered,
} from '@/utils/modelRoutingDiscovery'

function fakeStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value)
    },
  } satisfies Pick<Storage, 'getItem' | 'setItem'>
}

describe('model routing discovery storage', () => {
  it('starts undiscovered and can be marked discovered', () => {
    const storage = fakeStorage()

    expect(isModelRoutingDiscovered(storage)).toBe(false)
    expect(rememberModelRoutingDiscovered(storage)).toBe(true)
    expect(isModelRoutingDiscovered(storage)).toBe(true)
    expect(storage.getItem(MODEL_ROUTING_DISCOVERED_KEY)).toBe('1')
  })

  it('fails soft when storage is unavailable or throws', () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error('blocked')
      },
      setItem: () => {
        throw new Error('blocked')
      },
    } satisfies Pick<Storage, 'getItem' | 'setItem'>

    expect(isModelRoutingDiscovered(null)).toBe(false)
    expect(rememberModelRoutingDiscovered(null)).toBe(false)
    expect(isModelRoutingDiscovered(throwingStorage)).toBe(false)
    expect(rememberModelRoutingDiscovered(throwingStorage)).toBe(false)
  })
})
