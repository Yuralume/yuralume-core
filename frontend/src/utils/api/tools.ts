import axios from 'axios'
import type { ToolDescriptor } from '@/types/tool'

export async function listTools(): Promise<ToolDescriptor[]> {
  const { data } = await axios.get<ToolDescriptor[]>('/api/v1/tools')
  return data
}
