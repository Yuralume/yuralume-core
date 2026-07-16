import { describe, expect, it } from 'vitest'
import type { CharacterCardPreview } from '@/utils/api/characters'
import playerPanelSource from '@/components/PlayerCharacterCardPanel.vue?raw'
import adminPageSource from '@/pages/admin/CharactersAdminPage.vue?raw'
import wizardSource from '@/components/InitialRelationshipWizardModal.vue?raw'
import gallerySource from '@/components/CharacterCardGalleryModal.vue?raw'

/**
 * Phase 4 guards for the SillyTavern import UI. The frontend suite is
 * logic/source-level (no component mounting harness), so these assert the
 * load-bearing wiring directly on the SFC sources + the API type surface:
 *
 * - both import <input> elements accept ST carriers (.json / .png)
 * - the relationship wizard pre-fills known_context from a suggestion
 * - the gallery modal renders the ST drop-notice keyed off source_format
 * - the preview type carries the new ST-only fields
 */

describe('SillyTavern import accept attributes', () => {
  it('player card panel accepts SillyTavern .json/.png', () => {
    const src = playerPanelSource
    const accept = src.match(/accept="([^"]+)"/)?.[1] ?? ''
    expect(accept).toContain('.json')
    expect(accept).toContain('application/json')
    expect(accept).toContain('.png')
    expect(accept).toContain('image/png')
    // native path still supported
    expect(accept).toContain('.lumecard')
  })

  it('admin characters page accepts SillyTavern .json/.png', () => {
    const src = adminPageSource
    const accept = src.match(/accept="([^"]+)"/)?.[1] ?? ''
    expect(accept).toContain('.json')
    expect(accept).toContain('.png')
    expect(accept).toContain('.lumecard')
  })
})

describe('relationship wizard scenario pre-fill wiring', () => {
  it('accepts a suggestedKnownContext prop and seeds known_context from it', () => {
    const src = wizardSource
    expect(src).toContain('suggestedKnownContext')
    // The visible watcher pre-fills the form's known_context field.
    expect(src).toMatch(/fresh\.known_context\s*=\s*props\.suggestedKnownContext/)
  })

  it('player panel passes the card suggested_known_context into the wizard', () => {
    const src = playerPanelSource
    expect(src).toContain(':suggested-known-context')
    expect(src).toContain('suggested_known_context')
  })
})

describe('gallery drop-notice wiring', () => {
  it('renders the SillyTavern notice only for converted cards', () => {
    const src = gallerySource
    expect(src).toContain("source_format === 'sillytavern'")
    expect(src).toContain('playerSidebar.characterCards.sillytavern.title')
    expect(src).toContain('playerSidebar.characterCards.sillytavern.dropped.')
  })
})

describe('preview type carries SillyTavern fields', () => {
  it('exposes source_format / dropped_fields / suggested_known_context', () => {
    // A structurally-valid preview can set the ST-only fields.
    const card: Partial<CharacterCardPreview> = {
      source_format: 'sillytavern',
      dropped_fields: ['character_book', 'greetings'],
      suggested_known_context: 'You just walked into her cafe.',
    }
    expect(card.source_format).toBe('sillytavern')
    expect(card.dropped_fields).toContain('character_book')
    expect(card.suggested_known_context).toContain('cafe')
  })
})
