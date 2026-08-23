import type {
  AdvanceSessionResponse,
  BranchingDrama,
  BranchingDramaSummary,
  CreateBranchingDramaPayload,
  DramaNode,
  DramaSceneGallery,
  DramaSession,
  DramaToArcDraftPayload,
  InteractSessionPayload,
  InteractSessionResponse,
  QuotedActionPrice,
} from '@/types/branchingDrama'
import {
  ACTION_BRANCHING_DRAMA_ADVANCE,
  ACTION_BRANCHING_DRAMA_CREATE,
  ACTION_BRANCHING_DRAMA_INTERACT,
  ACTION_BRANCHING_DRAMA_SCENE_REGEN,
  useActionPricing,
} from '@/composables/useActionPricing'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'
import { authedFetch } from '@/utils/authedFetch'
import { apiErrorFromResponse } from '@/utils/api/insufficientCredits'

const BASE = '/api/v1'

/**
 * Attach the fixed price this client is quoting for a drama action (BD3).
 *
 * Same contract as the fusion transport's `withQuotedPrice`: the server binds
 * the charge to the number that was on the player's screen. Applied on the
 * transport rather than at each call site so every press carries the same
 * binding, and dropped entirely when nothing is quotable.
 */
function withQuotedPrice<T extends QuotedActionPrice>(
  payload: T,
  actionKey: string,
): T {
  const price = useActionPricing().priceOf(actionKey)
  if (price === null) return payload
  return { ...payload, quoted_price_cr: price.price_cr }
}

async function _req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authedFetch(`${BASE}${path}`, {
    headers: init.body
      ? { 'Content-Type': 'application/json', ...(init.headers || {}) }
      : init.headers,
    ...init,
  })
  // Start / interact / advance can each hit the hosted credit ceiling, so the
  // shared reader hands back the typed error instead of an opaque message.
  if (!res.ok) throw await apiErrorFromResponse(res)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export async function listBranchingDramas(
  limit = 50,
): Promise<BranchingDramaSummary[]> {
  return _req(`/branching-dramas?limit=${encodeURIComponent(limit)}`)
}

export async function getBranchingDrama(
  dramaId: string,
): Promise<BranchingDrama> {
  return _req(`/branching-dramas/${encodeURIComponent(dramaId)}`)
}

export async function createBranchingDrama(
  payload: CreateBranchingDramaPayload,
): Promise<BranchingDrama> {
  return _req(`/branching-dramas`, {
    method: 'POST',
    body: JSON.stringify(
      withQuotedPrice(payload, ACTION_BRANCHING_DRAMA_CREATE),
    ),
  })
}

export async function deleteBranchingDrama(
  dramaId: string,
): Promise<void> {
  await _req(`/branching-dramas/${encodeURIComponent(dramaId)}`, {
    method: 'DELETE',
  })
}

/**
 * 劇場圖集 (BD9) — the scene pictures this player walked past.
 *
 * Free to call: it reads what is already painted and stored, so unlike
 * every other endpoint in this module it carries no quote and raises no
 * charge. The response deliberately says nothing about the un-walked
 * pictures beyond how many there are.
 */
export async function getDramaGallery(
  dramaId: string,
): Promise<DramaSceneGallery> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/gallery`,
  )
}

export async function getDramaNode(
  dramaId: string,
  nodeId: string,
): Promise<DramaNode> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/nodes/${encodeURIComponent(nodeId)}`,
  )
}

export async function getNodeChildren(
  dramaId: string,
  nodeId: string,
): Promise<DramaNode[]> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/nodes/${encodeURIComponent(nodeId)}/children`,
  )
}

/**
 * Redraw one node's scene picture on the player's press (BD6).
 *
 * Its own action key, so the price chip next to the button quotes a redraw
 * and not the beat it sits on. The body carries only the quote — the same
 * optional shape as `advance`, so a deployment with nothing quotable posts
 * an empty object and the server falls back to its own cache.
 */
export async function regenerateSceneImage(
  dramaId: string,
  nodeId: string,
): Promise<DramaNode> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/nodes/${encodeURIComponent(nodeId)}/image/regenerate`,
    {
      method: 'POST',
      body: JSON.stringify(
        withQuotedPrice({}, ACTION_BRANCHING_DRAMA_SCENE_REGEN),
      ),
    },
  )
}

/**
 * Raise the curtain on a playthrough (FX1).
 *
 * Starting a session writes the opening beat, which is one generation and is
 * billed as one `branching_drama_advance` — so this press quotes the same
 * number the 「推進」 button does, and carries it in the same optional body
 * shape as `advance`: an unquotable deployment posts `{}`, which a backend
 * that declares no body simply ignores.
 */
export async function startSession(
  dramaId: string,
): Promise<DramaSession> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions`,
    {
      method: 'POST',
      body: JSON.stringify(withQuotedPrice({}, ACTION_BRANCHING_DRAMA_ADVANCE)),
    },
  )
}

export async function listSessions(
  dramaId: string,
): Promise<DramaSession[]> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions`,
  )
}

export async function getSession(
  dramaId: string,
  sessionId: string,
): Promise<DramaSession> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions/${encodeURIComponent(sessionId)}`,
  )
}

export async function interactSession(
  dramaId: string,
  sessionId: string,
  playerInput: string,
): Promise<InteractSessionResponse> {
  const payload: InteractSessionPayload = { player_input: playerInput }
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions/${encodeURIComponent(sessionId)}/interact`,
    {
      method: 'POST',
      body: JSON.stringify(
        withQuotedPrice(payload, ACTION_BRANCHING_DRAMA_INTERACT),
      ),
    },
  )
}

export async function advanceSession(
  dramaId: string,
  sessionId: string,
): Promise<AdvanceSessionResponse> {
  // Advance took no body before BD3 and the backend still accepts none; the
  // body exists only to carry the quote, so an unquotable deployment posts
  // exactly the empty object it always effectively did.
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions/${encodeURIComponent(sessionId)}/advance`,
    {
      method: 'POST',
      body: JSON.stringify(withQuotedPrice({}, ACTION_BRANCHING_DRAMA_ADVANCE)),
    },
  )
}

export async function endDramaSession(
  dramaId: string,
  sessionId: string,
): Promise<DramaSession> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions/${encodeURIComponent(sessionId)}/end`,
    { method: 'POST' },
  )
}

/**
 * Turn the line this player walked into an unsaved arc-template draft (BD7).
 *
 * No quote rides along: the conversion is billed on the legacy `arc_adapt`
 * feature key exactly like the fusion adaptation, so there is no fixed
 * price on screen for the charge to bind to.
 */
export async function adaptDramaSessionToArc(
  dramaId: string,
  sessionId: string,
  payload: DramaToArcDraftPayload = {},
): Promise<TemplateDraftPayload> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions/${encodeURIComponent(sessionId)}/adapt-to-arc`,
    { method: 'POST', body: JSON.stringify(payload) },
  )
}
