import type {
  AdvanceSessionResponse,
  BranchingDrama,
  BranchingDramaSummary,
  CreateBranchingDramaPayload,
  DramaNode,
  DramaSession,
  InteractSessionResponse,
} from '@/types/branchingDrama'
import { authedFetch } from '@/utils/authedFetch'
import { readErrorResponse } from '@/utils/api/httpError'

const BASE = '/api/v1'

async function _req<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await authedFetch(`${BASE}${path}`, {
    headers: init.body
      ? { 'Content-Type': 'application/json', ...(init.headers || {}) }
      : init.headers,
    ...init,
  })
  if (!res.ok) throw new Error(await readErrorResponse(res))
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
    body: JSON.stringify(payload),
  })
}

export async function deleteBranchingDrama(
  dramaId: string,
): Promise<void> {
  await _req(`/branching-dramas/${encodeURIComponent(dramaId)}`, {
    method: 'DELETE',
  })
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

export async function startSession(
  dramaId: string,
): Promise<DramaSession> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions`,
    { method: 'POST' },
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
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions/${encodeURIComponent(sessionId)}/interact`,
    {
      method: 'POST',
      body: JSON.stringify({ player_input: playerInput }),
    },
  )
}

export async function advanceSession(
  dramaId: string,
  sessionId: string,
): Promise<AdvanceSessionResponse> {
  return _req(
    `/branching-dramas/${encodeURIComponent(dramaId)}/sessions/${encodeURIComponent(sessionId)}/advance`,
    { method: 'POST' },
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
