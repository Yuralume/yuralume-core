import axios from 'axios'
import type { ChatAssistSuggestionsResponse } from '@/types/chatAssist'

export async function suggestChatAssistMessages(
  characterId: string,
  count = 4,
): Promise<ChatAssistSuggestionsResponse> {
  const { data } = await axios.post<ChatAssistSuggestionsResponse>(
    `/api/v1/characters/${encodeURIComponent(characterId)}/chat-assist/suggestions`,
    { count },
  )
  return data
}
