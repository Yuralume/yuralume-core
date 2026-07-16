export const CHAT_ASSIST_DISCOVERED_KEY = 'yuralume.chatAssist.discovered'
export const CHAT_ASSIST_HINT_DISMISSED_KEY = 'yuralume.chatAssist.hintDismissed'

export interface ChatAssistHintVisibilityInput {
  enabled: boolean
  assistOpen: boolean
  hasMessages: boolean
  inputEmpty: boolean
  discovered: boolean
  dismissed: boolean
}

type ChatAssistDiscoveryStorage = Pick<Storage, 'getItem' | 'setItem'>

export function shouldShowChatAssistHint(input: ChatAssistHintVisibilityInput): boolean {
  return input.enabled
    && !input.assistOpen
    && input.hasMessages
    && input.inputEmpty
    && !input.discovered
    && !input.dismissed
}

export function isChatAssistDiscovered(
  storage: ChatAssistDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    return storage.getItem(CHAT_ASSIST_DISCOVERED_KEY) === '1'
  } catch {
    return false
  }
}

export function rememberChatAssistDiscovered(
  storage: ChatAssistDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(CHAT_ASSIST_DISCOVERED_KEY, '1')
    return true
  } catch {
    return false
  }
}

export function isChatAssistHintDismissed(
  storage: ChatAssistDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    return storage.getItem(CHAT_ASSIST_HINT_DISMISSED_KEY) === '1'
  } catch {
    return false
  }
}

export function rememberChatAssistHintDismissed(
  storage: ChatAssistDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(CHAT_ASSIST_HINT_DISMISSED_KEY, '1')
    return true
  } catch {
    return false
  }
}
