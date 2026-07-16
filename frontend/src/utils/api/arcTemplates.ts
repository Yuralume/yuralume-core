/**
 * Arc template REST client.
 *
 * - Read endpoints: list / get bundled YAML templates
 * - Wizard endpoints (Phase 2.7 of SCENE_BEAT_PLAN): each method maps
 *   1:1 to a wizard step. The server is stateless; the wizard component
 *   owns the canonical draft.
 */

import axios from 'axios'
import type { ArcTemplate } from '@/types/arcTemplate'
import type {
  BeatContextPayload,
  BeatDraftPayload,
  CondensePremisePayload,
  SaveTemplateResponse,
  ScaffoldsResponse,
  SuggestBeatOptionsResponse,
  SuggestMetaResponse,
  TemplateDraftPayload,
} from '@/types/arcTemplateIntake'

const BASE = '/api/v1/arc-templates'

export async function listArcTemplates(options: {
  characterId?: string | null
} = {}): Promise<ArcTemplate[]> {
  const params = new URLSearchParams()
  if (options.characterId) {
    params.set('character_id', options.characterId)
  }
  const { data } = await axios.get<ArcTemplate[]>(BASE, { params })
  return data
}

export async function getArcTemplate(id: string): Promise<ArcTemplate> {
  const { data } = await axios.get<ArcTemplate>(`${BASE}/${id}`)
  return data
}

/**
 * Preview a template translated into the operator's primary language
 * (or an explicit `targetLanguage`). Read-only — the server never
 * persists the translation. Fail-soft on the backend: the authored
 * prose comes back when no translator is wired or the languages match.
 */
export async function previewArcTemplateTranslation(
  id: string,
  targetLanguage?: string | null,
): Promise<ArcTemplate> {
  const params = new URLSearchParams()
  if (targetLanguage) {
    params.set('target_language', targetLanguage)
  }
  const { data } = await axios.get<ArcTemplate>(
    `${BASE}/${id}/preview-translation`,
    { params },
  )
  return data
}

// ---------- Wizard --------------------------------------------------

export async function getArcTemplateScaffolds(): Promise<ScaffoldsResponse> {
  const { data } = await axios.get<ScaffoldsResponse>(`${BASE}/scaffolds`)
  return data
}

export async function suggestMeta(pitch: string): Promise<SuggestMetaResponse> {
  const { data } = await axios.post<SuggestMetaResponse>(
    `${BASE}/intake/suggest-meta`,
    { pitch },
  )
  return data
}

export async function condensePremise(
  payload: CondensePremisePayload,
): Promise<string> {
  const { data } = await axios.post<{ premise: string }>(
    `${BASE}/intake/condense-premise`,
    payload,
  )
  return data.premise
}

export async function suggestBeatOptions(
  context: BeatContextPayload,
): Promise<SuggestBeatOptionsResponse> {
  const { data } = await axios.post<SuggestBeatOptionsResponse>(
    `${BASE}/intake/suggest-beat-options`,
    { context },
  )
  return data
}

export async function generateBeatSummary(
  beat: BeatDraftPayload,
  context: BeatContextPayload,
): Promise<string> {
  const { data } = await axios.post<{ summary: string }>(
    `${BASE}/intake/generate-summary`,
    { beat, context },
  )
  return data.summary
}

export async function generateFullDraft(
  pitch: string,
  hint = '',
): Promise<TemplateDraftPayload | null> {
  const { data } = await axios.post<TemplateDraftPayload | null>(
    `${BASE}/intake/generate-full-draft`,
    { pitch, hint },
  )
  return data
}

/**
 * Save the wizard draft. Throws on 409 (id collision) so the caller
 * can ask the operator whether to overwrite. All other errors bubble.
 */
export async function saveArcTemplate(
  draft: TemplateDraftPayload,
  overwrite = false,
): Promise<SaveTemplateResponse> {
  const { data } = await axios.post<SaveTemplateResponse>(
    BASE,
    { draft, overwrite },
  )
  return data
}
