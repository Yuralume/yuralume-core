import axios from 'axios'
import type {
  OperatorProfile,
  UpdateOperatorProfileRequest,
} from '@/types/operator'

const BASE = '/api/v1/operator/profile'

export async function getOperatorProfile(): Promise<OperatorProfile> {
  const { data } = await axios.get<OperatorProfile>(BASE)
  return data
}

export async function updateOperatorProfile(
  payload: UpdateOperatorProfileRequest,
): Promise<OperatorProfile> {
  const { data } = await axios.put<OperatorProfile>(BASE, payload)
  return data
}
