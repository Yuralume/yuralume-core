import axios from 'axios'
import type { CreateGoalRequest, Goal, UpdateGoalRequest } from '@/types/goal'

const API = '/api/v1'

export async function listGoals(characterId: string): Promise<Goal[]> {
  const { data } = await axios.get<Goal[]>(`${API}/characters/${characterId}/goals`)
  return data
}

export async function createGoal(characterId: string, req: CreateGoalRequest): Promise<Goal> {
  const { data } = await axios.post<Goal>(`${API}/characters/${characterId}/goals`, req)
  return data
}

export async function updateGoal(goalId: string, req: UpdateGoalRequest): Promise<Goal> {
  const { data } = await axios.patch<Goal>(`${API}/goals/${goalId}`, req)
  return data
}

export async function deleteGoal(goalId: string): Promise<void> {
  await axios.delete(`${API}/goals/${goalId}`)
}
