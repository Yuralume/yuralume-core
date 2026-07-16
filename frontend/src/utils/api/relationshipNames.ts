import axios from 'axios'

export interface RelationshipNames {
  character_id: string
  operator_id: string
  user_address_name: string
  character_address_name: string
}

export interface RelationshipNamesPatch {
  user_address_name?: string
  character_address_name?: string
}

const BASE = '/api/v1'

export async function getRelationshipNames(
  characterId: string,
): Promise<RelationshipNames> {
  const { data } = await axios.get<RelationshipNames>(
    `${BASE}/characters/${encodeURIComponent(characterId)}/relationship-names`,
  )
  return data
}

export async function updateRelationshipNames(
  characterId: string,
  payload: RelationshipNamesPatch,
): Promise<RelationshipNames> {
  const { data } = await axios.patch<RelationshipNames>(
    `${BASE}/characters/${encodeURIComponent(characterId)}/relationship-names`,
    payload,
  )
  return data
}
