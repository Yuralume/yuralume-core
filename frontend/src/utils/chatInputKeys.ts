export function shouldSendChatInputOnKeydown(
  event: KeyboardEvent,
  composing = false,
): boolean {
  if (event.key !== 'Enter') return false
  if (event.shiftKey) return false
  if (composing || event.isComposing) return false

  const legacyCode = event.keyCode || (event as KeyboardEvent & { which?: number }).which
  if (legacyCode === 229) return false

  return true
}
