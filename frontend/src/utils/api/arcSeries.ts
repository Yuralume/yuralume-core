import axios from 'axios'

import type {
  ArcSeries,
  ArcSeriesPayload,
  ArcSeriesProgress,
  BindArcSeriesPayload,
  DraftNextSeasonPayload,
  ReorderArcSeriesPayload,
} from '@/types/arcSeries'
import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'

const BASE = '/api/v1/arc-series'

export async function listArcSeries(): Promise<ArcSeries[]> {
  const { data } = await axios.get<ArcSeries[]>(BASE)
  return data
}

export async function getArcSeries(seriesId: string): Promise<ArcSeries> {
  const { data } = await axios.get<ArcSeries>(`${BASE}/${seriesId}`)
  return data
}

export async function createArcSeries(
  payload: ArcSeriesPayload,
): Promise<ArcSeries> {
  const { data } = await axios.post<ArcSeries>(BASE, payload)
  return data
}

export async function updateArcSeries(
  seriesId: string,
  payload: ArcSeriesPayload,
): Promise<ArcSeries> {
  const { data } = await axios.patch<ArcSeries>(`${BASE}/${seriesId}`, payload)
  return data
}

export async function reorderArcSeries(
  seriesId: string,
  payload: ReorderArcSeriesPayload,
): Promise<ArcSeries> {
  const { data } = await axios.post<ArcSeries>(
    `${BASE}/${seriesId}/reorder`,
    payload,
  )
  return data
}

export async function deleteArcSeries(seriesId: string): Promise<void> {
  await axios.delete(`${BASE}/${seriesId}`)
}

export async function bindArcSeriesToCharacter(
  seriesId: string,
  payload: BindArcSeriesPayload,
): Promise<ArcSeries> {
  const { data } = await axios.post<ArcSeries>(
    `${BASE}/${seriesId}/bind-to-character`,
    payload,
  )
  return data
}

export async function clearArcSeriesBinding(characterId: string): Promise<void> {
  await axios.delete(`/api/v1/characters/${characterId}/arc-series-binding`)
}

export async function getArcSeriesProgress(
  characterId: string,
  seriesId: string,
): Promise<ArcSeriesProgress | null> {
  const { data } = await axios.get<ArcSeriesProgress | null>(
    `/api/v1/characters/${characterId}/arc-series-progress/${seriesId}`,
  )
  return data
}

export async function draftNextSeason(
  seriesId: string,
  payload: DraftNextSeasonPayload,
): Promise<TemplateDraftPayload> {
  const { data } = await axios.post<TemplateDraftPayload>(
    `${BASE}/${seriesId}/draft-next-season`,
    payload,
  )
  return data
}
