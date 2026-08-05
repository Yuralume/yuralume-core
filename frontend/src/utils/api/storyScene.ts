import type {
  StorySceneEndResponse,
  StorySceneOpenResponse,
  StorySceneSession,
} from '@/types/storyScene'
import {
  ACTION_STORY_SCENE_OPEN,
  useActionPricing,
} from '@/composables/useActionPricing'
import { authedFetch } from '@/utils/authedFetch'
import { formatErrorBody, readErrorBody } from '@/utils/api/httpError'
import { insufficientCreditsFromBody } from '@/utils/api/insufficientCredits'
import { priceChangedFromBody } from '@/utils/api/priceChanged'

const BASE = '/api/v1'

/**
 * A structured refusal from the story-scene routes.
 *
 * Every one of them answers `{"detail": {"code", "message"}}`, and the code is
 * the only part the UI may key off: the message is server-side English meant
 * for logs, while what the player reads is resolved from the trilingual
 * catalog by `storySceneErrorKey`.
 */
export class StorySceneError extends Error {
  code: string
  statusCode: number

  constructor(input: { code: string; message?: string; statusCode: number }) {
    super(input.message || input.code)
    this.name = 'StorySceneError'
    this.code = input.code
    this.statusCode = input.statusCode
  }
}

function structuredCode(detail: unknown): string | null {
  if (typeof detail !== 'object' || detail === null) return null
  const code = (detail as { code?: unknown }).code
  return typeof code === 'string' && code ? code : null
}

function structuredMessage(detail: unknown): string | undefined {
  if (typeof detail !== 'object' || detail === null) return undefined
  const message = (detail as { message?: unknown }).message
  return typeof message === 'string' && message ? message : undefined
}

/**
 * Turn a non-ok response into the most specific error we can name.
 *
 * The billing types come first: the button carries a price (SC3-C), so an
 * out-of-credits or moved-price refusal has to arrive typed the way every
 * other money surface in the SPA expects it, not flattened into a generic
 * scene error the player cannot act on.
 */
async function sceneErrorFromResponse(res: Response): Promise<Error> {
  const body = await readErrorBody(res)
  const credits = insufficientCreditsFromBody(body, res.status)
  if (credits) return credits
  const priceChanged = priceChangedFromBody(body, res.status)
  if (priceChanged) return priceChanged
  const detail = (body as { detail?: unknown } | null)?.detail
  const code = structuredCode(detail)
  if (code) {
    return new StorySceneError({
      code,
      message: structuredMessage(detail),
      statusCode: res.status,
    })
  }
  return new Error(formatErrorBody(body, res))
}

async function _req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authedFetch(`${BASE}${path}`, {
    headers: init.body
      ? { 'Content-Type': 'application/json', ...(init.headers || {}) }
      : init.headers,
    ...init,
  })
  if (!res.ok) throw await sceneErrorFromResponse(res)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/**
 * Raise the curtain: open a scene for this character.
 *
 * The body carries the price this screen was quoting (R-9 quote binding), so
 * the charge is bound to the number the player actually saw rather than to
 * whatever the server's own cache holds — under several replicas, or right
 * after a back-office edit, those are not the same number. Nothing quotable
 * (self-host, a usage-billed tier, a price list that has not loaded) means no
 * body at all, which is exactly what SC1-A shipped and what the route still
 * accepts. Under-quoting buys nothing: the server answers `409 price_changed`.
 */
export async function openStoryScene(
  characterId: string,
): Promise<StorySceneOpenResponse> {
  const price = useActionPricing().priceOf(ACTION_STORY_SCENE_OPEN)
  return _req(
    `/characters/${encodeURIComponent(characterId)}/story-scene`,
    {
      method: 'POST',
      ...(price === null
        ? {}
        : { body: JSON.stringify({ quoted_price_cr: price.price_cr }) }),
    },
  )
}

/**
 * The character's current scene, or `null` when none is open.
 *
 * Called when the thread opens so a player who reloads mid-scene comes back
 * to the scene they left, rather than to a button that would refuse them.
 */
export async function getStoryScene(
  characterId: string,
): Promise<StorySceneSession | null> {
  return _req(`/characters/${encodeURIComponent(characterId)}/story-scene`)
}

/**
 * Bring the curtain down early.
 *
 * `sessionId` is sent when we know which scene we are closing so a stale tab
 * cannot end a scene that has since been replaced.
 */
export async function endStoryScene(
  characterId: string,
  sessionId: string | null = null,
): Promise<StorySceneEndResponse> {
  return _req(
    `/characters/${encodeURIComponent(characterId)}/story-scene/end`,
    {
      method: 'POST',
      body: JSON.stringify({ session_id: sessionId }),
    },
  )
}
