import { describe, expect, it } from 'vitest'

import characterBackupPanelSource from '@/components/CharacterBackupPanel.vue?raw'
import characterBackupRestorePanelSource from '@/components/CharacterBackupRestorePanel.vue?raw'
import playerCardPanelSource from '@/components/PlayerCharacterCardPanel.vue?raw'
import playerSidebarSource from '@/components/PlayerSidebar.vue?raw'
import adminPageSource from '@/pages/admin/CharactersAdminPage.vue?raw'

/**
 * A player with zero characters used to have no way to reach the CB4
 * restore flow at all: it only lived inside `CharacterBackupPanel`, mounted
 * under character-scoped settings, which requires a `character` prop and is
 * therefore unreachable before any character exists. `CharacterBackupPanel`
 * is now export-only, and restore moved to the character-agnostic
 * `CharacterBackupRestorePanel`, mounted next to the other "add a
 * character" surfaces. These guard both halves of that split: the entry
 * points are reachable, and the old panel no longer duplicates them.
 *
 * Source-level (no DOM/SSR mounting harness in this repo, see
 * `sillytavernImportUi.test.ts`) — `CharacterBackupRestorePanel` always
 * mounts `CharacterBackupPasswordModal` as a child even while closed, and
 * that modal's `visible` watcher touches `window` unconditionally
 * (`immediate: true`), which a plain Node render environment cannot supply.
 */

describe('CharacterBackupRestorePanel is self-sufficient (no character prop)', () => {
  it('takes no character prop and emits the same "imported" shape', () => {
    expect(characterBackupRestorePanelSource).not.toContain('defineProps')
    expect(characterBackupRestorePanelSource).toContain('imported: [character: Character]')
  })

  it('wires the .lumebackup file input and the import-mode password modal', () => {
    expect(characterBackupRestorePanelSource).toContain('accept=".lumebackup"')
    expect(characterBackupRestorePanelSource).toContain("mode=\"import\"")
    expect(characterBackupRestorePanelSource).toContain('useCharacterBackupImport')
  })
})

describe('CharacterBackupPanel no longer duplicates the restore flow', () => {
  it('drops the .lumebackup file input (export only now)', () => {
    expect(characterBackupPanelSource).not.toContain('accept=".lumebackup"')
  })

  it('no longer wires useCharacterBackupImport', () => {
    expect(characterBackupPanelSource).not.toContain('useCharacterBackupImport')
  })
})

describe('backup restore is reachable with zero characters', () => {
  it('the character-card panel (mounted before any character exists) hosts the restore panel', () => {
    expect(playerCardPanelSource).toContain('CharacterBackupRestorePanel')
  })

  it('the zero-character onboarding branch keeps that section open by default', () => {
    const emptyStateBranch = playerSidebarSource.match(
      /<CollapsibleSection[\s\S]*?<PlayerCharacterCardPanel\s+ref="emptyCharacterCardPanel"/,
    )
    expect(emptyStateBranch).not.toBeNull()
    expect(emptyStateBranch?.[0]).toContain('default-open="true"')
  })

  it('the admin create-character card hosts the restore panel alongside card import', () => {
    expect(adminPageSource).toContain('CharacterBackupRestorePanel')
  })
})
