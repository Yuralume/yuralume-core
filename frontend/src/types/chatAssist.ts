export interface ChatAssistSuggestion {
  text: string
  reason?: string | null
}

export interface ChatAssistSuggestionsResponse {
  suggestions: ChatAssistSuggestion[]
}
