import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { StorySceneSession } from '@/types/storyScene'
import { InsufficientCreditsError } from '@/utils/api/insufficientCredits'
import { PriceChangedError } from '@/utils/api/priceChanged'

/**
 * The scene state machine (plan SC, ticket SC2).
 *
 * What is asserted here is mostly about *not lying to the player*: the button
 * never claims a scene the server refused, a slow answer for a character the
 * player already left never installs itself onto the new thread, and a failed
 * state read leaves the ordinary button rather than an invented verdict.
 */
// `authedFetch` reaches for `localStorage` at import time; nothing here
// issues a request, so the transport is stubbed rather than the world.
vi.mock('@/utils/authedFetch', () => ({
  authedFetch: vi.fn(),
}))

vi.mock('@/utils/api/storyScene', async () => {
  const actual = await vi.importActual<
    typeof import('@/utils/api/storyScene')
  >('@/utils/api/storyScene')
  return {
    ...actual,
    openStoryScene: vi.fn(),
    getStoryScene: vi.fn(),
    endStoryScene: vi.fn(),
  }
})

const {
  StorySceneError,
  openStoryScene,
  getStoryScene,
  endStoryScene,
} = await import('@/utils/api/storyScene')
const { useStoryScene } = await import('@/composables/useStoryScene')

const mockedOpen = vi.mocked(openStoryScene)
const mockedGet = vi.mocked(getStoryScene)
const mockedEnd = vi.mocked(endStoryScene)

function session(overrides: Partial<StorySceneSession> = {}): StorySceneSession {
  return {
    id: 'scene-1',
    character_id: 'char-1',
    conversation_id: 'conv-1',
    status: 'open',
    source_layer: 'beat',
    arc_id: null,
    beat_id: null,
    title: 'A rooftop solo',
    location: 'Rooftop',
    mood: 'Unspoken',
    scene_type: null,
    dramatic_question: null,
    opened_at: '2026-08-01T00:00:00Z',
    last_activity_at: '2026-08-01T00:00:00Z',
    closed_at: null,
    closed_reason: null,
    ...overrides,
  }
}

function narration(content: string) {
  return {
    role: 'assistant' as const,
    content,
    attachments: [],
    turn_record_id: null,
    kind: 'scene_narration' as const,
  }
}

/**
 * Let the state machine walk its own awaits without settling a gated
 * request, so a test can act at a point the composable is genuinely at.
 */
async function flushMicrotasks(times = 8): Promise<void> {
  for (let i = 0; i < times; i += 1) await Promise.resolve()
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('opening a scene', () => {
  it('holds the session and the first chips on success', async () => {
    mockedOpen.mockResolvedValueOnce({
      session: session(),
      narration: narration('The doors open.'),
      character_message: { role: 'assistant', content: 'You came.' },
      suggested_actions: [{ text: 'Sit beside her' }, { text: 'Say nothing' }],
    })
    const scene = useStoryScene()

    const response = await scene.open('char-1')

    expect(response).not.toBeNull()
    expect(scene.isOpen.value).toBe(true)
    expect(scene.session.value?.title).toBe('A rooftop solo')
    expect(scene.suggestedActions.value).toHaveLength(2)
    expect(scene.errorKey.value).toBeNull()
    expect(scene.opening.value).toBe(false)
  })

  it('claims no scene when the server refused one', async () => {
    mockedOpen.mockRejectedValueOnce(
      new StorySceneError({ code: 'no_material', statusCode: 409 }),
    )
    const scene = useStoryScene()

    const response = await scene.open('char-1')

    expect(response).toBeNull()
    expect(scene.isOpen.value).toBe(false)
    expect(scene.errorKey.value).toBe('chat.storyScene.errors.noMaterial')
  })

  it('withdraws the button where scenes are not offered', async () => {
    mockedOpen.mockRejectedValueOnce(
      new StorySceneError({ code: 'story_scene_unavailable', statusCode: 403 }),
    )
    const scene = useStoryScene()

    await scene.open('char-1')

    expect(scene.unavailable.value).toBe(true)
    expect(scene.errorKey.value).toBe('chat.storyScene.errors.unavailable')
  })

  it('refuses a second press while the first is still in flight', async () => {
    const inFlight: { reject?: (reason: unknown) => void } = {}
    mockedOpen.mockImplementationOnce(() => new Promise((_, reject) => {
      inFlight.reject = reject
    }))
    const scene = useStoryScene()

    const first = scene.open('char-1')
    const second = await scene.open('char-1')

    expect(second).toBeNull()
    expect(mockedOpen).toHaveBeenCalledTimes(1)
    inFlight.reject?.(new Error('cancelled'))
    await first
  })

  it('hands the money refusals back to the surface that owns them', async () => {
    // SC3-C: "you are out of Lumes" needs a card with a top-up link and
    // "the price moved" needs the price list re-pulled. Flattening either
    // into a scene error would swap something the player can act on for a
    // line they cannot — so both are rethrown, with the scene untouched.
    for (const error of [
      new InsufficientCreditsError({ message: 'out of credits' }),
      new PriceChangedError({ message: 'moved', currentPriceCr: 8 }),
    ]) {
      mockedOpen.mockRejectedValueOnce(error)
      const scene = useStoryScene()

      await expect(scene.open('char-1')).rejects.toBe(error)

      expect(scene.isOpen.value).toBe(false)
      expect(scene.errorKey.value).toBeNull()
      expect(scene.unavailable.value).toBe(false)
      // and the button is pressable again straight away
      expect(scene.opening.value).toBe(false)
    }
  })

  it('will not open a second scene over a running one', async () => {
    mockedGet.mockResolvedValueOnce(session())
    const scene = useStoryScene()
    await scene.restore('char-1')

    await scene.open('char-1')

    expect(mockedOpen).not.toHaveBeenCalled()
  })
})

describe('restoring a scene', () => {
  it('comes back to the scene the player left', async () => {
    mockedGet.mockResolvedValueOnce(session())
    const scene = useStoryScene()

    await scene.restore('char-1')

    expect(scene.isOpen.value).toBe(true)
    expect(scene.session.value?.location).toBe('Rooftop')
  })

  it('treats a closed session as no scene', async () => {
    mockedGet.mockResolvedValueOnce(
      session({ status: 'closed', closed_reason: 'timeout' }),
    )
    const scene = useStoryScene()

    await scene.restore('char-1')

    expect(scene.isOpen.value).toBe(false)
    expect(scene.session.value).toBeNull()
  })

  it('says nothing when the state cannot be read', async () => {
    // Not being able to read is not something the player did. The honest
    // rendering is the ordinary button, which produces a real answer.
    mockedGet.mockRejectedValueOnce(new Error('offline'))
    const scene = useStoryScene()

    await scene.restore('char-1')

    expect(scene.errorKey.value).toBeNull()
    expect(scene.isOpen.value).toBe(false)
    expect(scene.unavailable.value).toBe(false)
  })

  it('never installs a stale answer onto the character now on screen', async () => {
    const inFlight: { resolve?: (value: StorySceneSession | null) => void } = {}
    mockedGet.mockImplementationOnce(() => new Promise((resolve) => {
      inFlight.resolve = resolve
    }))
    mockedGet.mockResolvedValueOnce(null)
    const scene = useStoryScene()

    const stale = scene.restore('char-1')
    await scene.restore('char-2')
    inFlight.resolve?.(session({ character_id: 'char-1' }))
    await stale

    expect(scene.session.value).toBeNull()
  })
})

describe('ending a scene', () => {
  it('closes the scene and drops the chips', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedEnd.mockResolvedValueOnce({
      session: session({ status: 'closed', closed_reason: 'manual' }),
      closing_narration: narration('The roof empties out.'),
    })
    const scene = useStoryScene()
    await scene.restore('char-1')
    scene.setSuggestedActions([{ text: 'Stay' }])

    const response = await scene.end('char-1')

    expect(mockedEnd).toHaveBeenCalledWith('char-1', 'scene-1')
    expect(response?.closing_narration?.content).toBe('The roof empties out.')
    expect(scene.isOpen.value).toBe(false)
    expect(scene.suggestedActions.value).toEqual([])
    // The session itself is kept so the frame above keeps its heading.
    expect(scene.session.value?.status).toBe('closed')
  })

  it('leaves the scene running when ending is not wired up yet', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedEnd.mockRejectedValueOnce(new StorySceneError({
      code: 'scene_end_not_implemented',
      statusCode: 501,
    }))
    const scene = useStoryScene()
    await scene.restore('char-1')

    const response = await scene.end('char-1')

    expect(response).toBeNull()
    expect(scene.isOpen.value).toBe(true)
    expect(scene.errorKey.value).toBe('chat.storyScene.errors.endNotAvailable')
  })

  it('does nothing when there is no scene to end', async () => {
    const scene = useStoryScene()

    await scene.end('char-1')

    expect(mockedEnd).not.toHaveBeenCalled()
  })
})

describe('a refusal that says the screen is out of date', () => {
  // M7: without a resync the player is left on a scene banner whose End
  // button can only produce the same 409 for ever — the state machine has to
  // treat "that is not the scene running" as an instruction to go and read.

  it('lets go of a scene the server says already ended', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedEnd.mockRejectedValueOnce(new StorySceneError({
      code: 'no_open_scene',
      statusCode: 409,
    }))
    mockedGet.mockResolvedValueOnce(null)
    const scene = useStoryScene()
    await scene.restore('char-1')
    scene.setSuggestedActions([{ text: 'Stay' }])

    const response = await scene.end('char-1')

    expect(response).toBeNull()
    expect(mockedGet).toHaveBeenCalledTimes(2)
    expect(scene.session.value).toBeNull()
    expect(scene.isOpen.value).toBe(false)
    expect(scene.suggestedActions.value).toEqual([])
    // The player pressed a button, so they are still told what happened.
    expect(scene.errorKey.value).toBe('chat.storyScene.errors.endAlreadyEnded')
    expect(scene.ending.value).toBe(false)
  })

  it('switches to the scene that replaced the one on screen', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedEnd.mockRejectedValueOnce(new StorySceneError({
      code: 'scene_session_mismatch',
      statusCode: 409,
    }))
    mockedGet.mockResolvedValueOnce(session({ id: 'scene-2', title: 'A late train' }))
    const scene = useStoryScene()
    await scene.restore('char-1')
    scene.setSuggestedActions([{ text: 'Stay' }])

    await scene.end('char-1')

    expect(scene.isOpen.value).toBe(true)
    expect(scene.session.value?.id).toBe('scene-2')
    expect(scene.session.value?.title).toBe('A late train')
    // The chips were written for the scene that is now gone.
    expect(scene.suggestedActions.value).toEqual([])
    expect(scene.errorKey.value).toBe('chat.storyScene.errors.endSceneChanged')
  })

  it('leaves the scene alone for every other end failure', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedEnd.mockRejectedValueOnce(new StorySceneError({
      code: 'scene_end_not_implemented',
      statusCode: 501,
    }))
    const scene = useStoryScene()
    await scene.restore('char-1')

    await scene.end('char-1')

    // Only the restore read. A 501 or a dropped connection says nothing
    // about which scene is running, and re-reading on every failure would
    // turn an outage into a second request per press.
    expect(mockedGet).toHaveBeenCalledTimes(1)
    expect(scene.isOpen.value).toBe(true)
  })

  it('keeps what it last knew when the resync itself fails', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedEnd.mockRejectedValueOnce(new StorySceneError({
      code: 'no_open_scene',
      statusCode: 409,
    }))
    mockedGet.mockRejectedValueOnce(new Error('offline'))
    const scene = useStoryScene()
    await scene.restore('char-1')

    await scene.end('char-1')

    expect(scene.isOpen.value).toBe(true)
    expect(scene.errorKey.value).toBe('chat.storyScene.errors.endAlreadyEnded')
  })

  it('adopts the scene another tab opened rather than apologising for it', async () => {
    // `scene_in_progress` can only reach this branch when the panel does not
    // know about the scene — otherwise the open guard refuses the press. So
    // the scene is real and invisible here, and the refusal alone would give
    // the player no way to see or close it.
    mockedOpen.mockRejectedValueOnce(new StorySceneError({
      code: 'scene_in_progress',
      statusCode: 409,
    }))
    mockedGet.mockResolvedValueOnce(session({ id: 'scene-2' }))
    const scene = useStoryScene()

    const response = await scene.open('char-1')

    expect(response).toBeNull()
    expect(scene.isOpen.value).toBe(true)
    expect(scene.session.value?.id).toBe('scene-2')
    expect(scene.errorKey.value).toBe('chat.storyScene.errors.inProgress')
    expect(scene.opening.value).toBe(false)
  })

  it('does not resync when scenes are not offered at all', async () => {
    mockedOpen.mockRejectedValueOnce(new StorySceneError({
      code: 'story_scene_unavailable',
      statusCode: 403,
    }))
    const scene = useStoryScene()

    await scene.open('char-1')

    expect(mockedGet).not.toHaveBeenCalled()
    expect(scene.unavailable.value).toBe(true)
  })

  it('never installs the resync onto a character the player has left', async () => {
    const gate: { resolve?: (value: StorySceneSession | null) => void } = {}
    mockedGet.mockResolvedValueOnce(session())
    mockedEnd.mockRejectedValueOnce(new StorySceneError({
      code: 'scene_session_mismatch',
      statusCode: 409,
    }))
    mockedGet.mockImplementationOnce(() => new Promise((resolve) => {
      gate.resolve = resolve
    }))
    mockedGet.mockResolvedValueOnce(null)
    const scene = useStoryScene()
    await scene.restore('char-1')

    const stale = scene.end('char-1')
    await flushMicrotasks()
    // Pinned so a timing change fails here rather than silently making the
    // rest of this test assert nothing.
    expect(mockedGet).toHaveBeenCalledTimes(2)

    await scene.restore('char-2')
    gate.resolve?.(session({ id: 'scene-2' }))
    await stale

    expect(scene.session.value).toBeNull()
  })
})

describe('chips', () => {
  it('only exist inside a scene', async () => {
    const scene = useStoryScene()

    scene.setSuggestedActions([{ text: 'Sit beside her' }])
    expect(scene.suggestedActions.value).toEqual([])

    mockedGet.mockResolvedValueOnce(session())
    await scene.restore('char-1')
    scene.setSuggestedActions([{ text: 'Sit beside her' }])
    expect(scene.suggestedActions.value).toHaveLength(1)

    // A turn that produced none clears the previous turn's suggestions
    // rather than leaving a stale action on screen.
    scene.setSuggestedActions(null)
    expect(scene.suggestedActions.value).toEqual([])
  })

  it('goes away with the character', async () => {
    mockedGet.mockResolvedValueOnce(session())
    const scene = useStoryScene()
    await scene.restore('char-1')
    scene.setSuggestedActions([{ text: 'Stay' }])

    scene.clear()

    expect(scene.suggestedActions.value).toEqual([])
    expect(scene.session.value).toBeNull()
    expect(scene.unavailable.value).toBe(false)
  })
})

describe('adopting the close an in-scene turn brought back (SC1-D)', () => {
  it('takes the closed session straight off the reply', async () => {
    mockedGet.mockResolvedValueOnce(session())
    const scene = useStoryScene()
    await scene.restore('char-1')
    scene.setSuggestedActions([{ text: 'Stay' }])

    const adopted = scene.adoptClosed(
      session({ status: 'closed', closed_reason: 'resolved' }),
    )

    expect(adopted).toBe(true)
    expect(scene.isOpen.value).toBe(false)
    expect(scene.suggestedActions.value).toEqual([])
    // ...and the session is kept, so the frame above keeps its heading —
    // the same shape the manual end leaves behind.
    expect(scene.session.value?.closed_reason).toBe('resolved')
    // The whole point: no second request to find this out.
    expect(mockedGet).toHaveBeenCalledTimes(1)
  })

  it('ignores a turn that did not end anything', async () => {
    mockedGet.mockResolvedValueOnce(session())
    const scene = useStoryScene()
    await scene.restore('char-1')

    expect(scene.adoptClosed(null)).toBe(false)
    expect(scene.adoptClosed(undefined)).toBe(false)
    expect(scene.adoptClosed(session())).toBe(false)
    expect(scene.isOpen.value).toBe(true)
  })

  it('refuses to close a scene it is not the one on screen', async () => {
    // A stale tab, or a scene closed and reopened on another device: the
    // reply names a scene this panel is not showing.
    mockedGet.mockResolvedValueOnce(session({ id: 'scene-2' }))
    const scene = useStoryScene()
    await scene.restore('char-1')

    const adopted = scene.adoptClosed(session({ status: 'closed' }))

    expect(adopted).toBe(false)
    expect(scene.isOpen.value).toBe(true)
  })
})

describe('syncing after a turn', () => {
  it('lets go of a scene the story already resolved', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedGet.mockResolvedValueOnce(null)
    const scene = useStoryScene()
    await scene.restore('char-1')
    scene.setSuggestedActions([{ text: 'Stay' }])

    await scene.sync('char-1')

    expect(scene.isOpen.value).toBe(false)
    expect(scene.suggestedActions.value).toEqual([])
  })

  it('keeps what it last knew when the read fails', async () => {
    mockedGet.mockResolvedValueOnce(session())
    mockedGet.mockRejectedValueOnce(new Error('offline'))
    const scene = useStoryScene()
    await scene.restore('char-1')

    await scene.sync('char-1')

    expect(scene.isOpen.value).toBe(true)
    expect(scene.errorKey.value).toBeNull()
  })

  it('is a no-op outside a scene', async () => {
    const scene = useStoryScene()

    await scene.sync('char-1')

    expect(mockedGet).not.toHaveBeenCalled()
  })
})

describe('isolation between threads', () => {
  it('gives each panel its own state, not a shared module ref', async () => {
    // A module-level singleton would leak one character's scene into the
    // next thread the player opens.
    mockedGet.mockResolvedValueOnce(session())
    const first = useStoryScene()
    const second = useStoryScene()

    await first.restore('char-1')

    expect(first.isOpen.value).toBe(true)
    expect(second.isOpen.value).toBe(false)
  })
})
