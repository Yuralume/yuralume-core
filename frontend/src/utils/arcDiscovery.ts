export const ARC_DISCOVERY_DISMISS_PREFIX = 'yuralume.arcDiscovery.dismissed.'
export const ARC_DISCOVERY_STUDIO_COACHMARK_KEY =
  'yuralume.arcDiscovery.studioCoachmark.dismissed'

/**
 * One-shot coachmark shown the first time a finished fusion story reveals
 * its exit hub (Creator Studio C1-P2). User-wide, not per-character —
 * once the creator learns "a finished work can perform / continue /
 * branch", we don't repeat it.
 */
export const STUDIO_EXIT_HUB_COACHMARK_KEY =
  'yuralume.studio.exitHubCoachmark.dismissed'

export interface ArcDiscoveryCharacterState {
  id: string | null | undefined
  arc_template_id?: string | null
  arc_series_id?: string | null
}

export interface ArcDiscoveryVisibilityInput {
  character: ArcDiscoveryCharacterState | null | undefined
  hasActiveArc: boolean
  dismissed: boolean
}

type ArcDiscoveryStorage = Pick<Storage, 'getItem' | 'setItem'>

export function arcDiscoveryDismissKey(characterId: string): string {
  return `${ARC_DISCOVERY_DISMISS_PREFIX}${characterId}`
}

export function hasArcDiscoveryBinding(
  character: ArcDiscoveryCharacterState | null | undefined,
): boolean {
  return Boolean(character?.arc_template_id || character?.arc_series_id)
}

export function shouldShowArcDiscovery(input: ArcDiscoveryVisibilityInput): boolean {
  if (!input.character?.id) return false
  if (input.hasActiveArc) return false
  if (input.dismissed) return false
  return !hasArcDiscoveryBinding(input.character)
}

export function isArcDiscoveryDismissed(
  storage: ArcDiscoveryStorage | null | undefined,
  characterId: string | null | undefined,
): boolean {
  if (!storage || !characterId) return false
  try {
    return storage.getItem(arcDiscoveryDismissKey(characterId)) === '1'
  } catch {
    return false
  }
}

export function rememberArcDiscoveryDismissed(
  storage: ArcDiscoveryStorage | null | undefined,
  characterId: string | null | undefined,
): boolean {
  if (!storage || !characterId) return false
  try {
    storage.setItem(arcDiscoveryDismissKey(characterId), '1')
    return true
  } catch {
    return false
  }
}

export function isStudioCoachmarkDismissed(
  storage: ArcDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    return storage.getItem(ARC_DISCOVERY_STUDIO_COACHMARK_KEY) === '1'
  } catch {
    return false
  }
}

export function rememberStudioCoachmarkDismissed(
  storage: ArcDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(ARC_DISCOVERY_STUDIO_COACHMARK_KEY, '1')
    return true
  } catch {
    return false
  }
}

export function isExitHubCoachmarkDismissed(
  storage: ArcDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    return storage.getItem(STUDIO_EXIT_HUB_COACHMARK_KEY) === '1'
  } catch {
    return false
  }
}

export function rememberExitHubCoachmarkDismissed(
  storage: ArcDiscoveryStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(STUDIO_EXIT_HUB_COACHMARK_KEY, '1')
    return true
  } catch {
    return false
  }
}
