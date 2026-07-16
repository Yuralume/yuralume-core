export type SettingsScope = 'personal' | 'character'

export interface SettingsScopeState {
  current: SettingsScope
  hasCharacter: boolean
}

export function isCharacterScopeAvailable(hasCharacter: boolean): boolean {
  return hasCharacter
}

export function resolveSettingsScope(state: SettingsScopeState): SettingsScope {
  if (state.current === 'character' && !state.hasCharacter) {
    return 'personal'
  }
  return state.current
}
