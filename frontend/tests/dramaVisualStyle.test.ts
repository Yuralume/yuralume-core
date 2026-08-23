/**
 * BD10 — the create form's art-direction picker follows the cast until the
 * player answers, and never after.
 *
 * Both halves are load-bearing and only one of them is obvious. A picker
 * that ignores the cast is merely annoying; a picker that keeps re-deriving
 * after the player chose is a drama drawn in a style they explicitly
 * rejected, discovered only once the pictures come back — and paid for.
 */

import { describe, expect, it } from 'vitest'
import {
  chooseVisualStyle,
  defaultVisualStyleForCast,
  initialVisualStyleSelection,
  syncVisualStyleWithCast,
  type StyledCharacter,
} from '../src/utils/dramaVisualStyle'
import pageSource from '../src/pages/BranchingDramaPage.vue?raw'

const CAST: StyledCharacter[] = [
  { id: 'c-anime', visual_generation_style: 'anime' },
  { id: 'c-real', visual_generation_style: 'realistic' },
  { id: 'c-inherit', visual_generation_style: '' },
  { id: 'c-null', visual_generation_style: null },
]

describe('defaultVisualStyleForCast', () => {
  it('suggests the first cast member’s own style', () => {
    expect(defaultVisualStyleForCast(['c-real', 'c-anime'], CAST)).toBe('realistic')
    expect(defaultVisualStyleForCast(['c-anime', 'c-real'], CAST)).toBe('anime')
  })

  it('reads only the first — a later cast member never overrules it', () => {
    // The bug this ticket removes was exactly "some other character in the
    // list decided the look". The suggestion must not reintroduce it.
    expect(defaultVisualStyleForCast(['c-inherit', 'c-real'], CAST)).toBe('anime')
  })

  it('falls back to the product default when there is nothing to read', () => {
    // `''` means inherit, and the browser cannot see the owner preference
    // the backend would consult next — so this is a suggestion, and the
    // backend re-resolves it properly if the player leaves it alone.
    expect(defaultVisualStyleForCast(['c-inherit'], CAST)).toBe('anime')
    expect(defaultVisualStyleForCast(['c-null'], CAST)).toBe('anime')
    expect(defaultVisualStyleForCast([], CAST)).toBe('anime')
    expect(defaultVisualStyleForCast(['c-unknown'], CAST)).toBe('anime')
    // Characters not loaded yet: the cast ids alone say nothing.
    expect(defaultVisualStyleForCast(['c-real'], [])).toBe('anime')
  })
})

describe('syncVisualStyleWithCast', () => {
  it('follows the cast while the player has not answered', () => {
    let selection = initialVisualStyleSelection()
    expect(selection).toEqual({ style: 'anime', touched: false })

    selection = syncVisualStyleWithCast(selection, ['c-real'], CAST)
    expect(selection).toEqual({ style: 'realistic', touched: false })

    // …and keeps following as the cast changes again.
    selection = syncVisualStyleWithCast(selection, ['c-anime', 'c-real'], CAST)
    expect(selection).toEqual({ style: 'anime', touched: false })
  })

  it('never overrules a player who picked one', () => {
    const chosen = chooseVisualStyle('realistic')
    expect(chosen).toEqual({ style: 'realistic', touched: true })

    const after = syncVisualStyleWithCast(chosen, ['c-anime'], CAST)
    expect(after).toEqual({ style: 'realistic', touched: true })
  })

  it('keeps a deliberate choice that happens to equal the suggestion', () => {
    // Picking anime while anime was already suggested still counts as
    // answering: adding a realistic lead afterwards must not move it.
    const chosen = chooseVisualStyle('anime')
    const after = syncVisualStyleWithCast(chosen, ['c-real'], CAST)
    expect(after.style).toBe('anime')
  })

  it('returns the same object when nothing changed, so a watch cannot loop', () => {
    const selection = syncVisualStyleWithCast(
      initialVisualStyleSelection(), ['c-anime'], CAST,
    )
    expect(syncVisualStyleWithCast(selection, ['c-anime'], CAST)).toBe(selection)

    const chosen = chooseVisualStyle('realistic')
    expect(syncVisualStyleWithCast(chosen, ['c-anime'], CAST)).toBe(chosen)
  })
})

describe('the create page wiring', () => {
  it('sends the chosen style and resets the picker after creating', () => {
    // A form that kept `touched` across creations would silently apply one
    // drama's answer to the next one's cast.
    expect(pageSource).toContain('visual_style: visualStyle.value')
    expect(pageSource).toContain(
      'visualStyleSelection.value = initialVisualStyleSelection()',
    )
  })

  it('hides the detail row for a drama created before the slot existed', () => {
    // `visual_style` is null there and the drama really is styled off its
    // first character, so printing a name would be a guess shown as a fact.
    expect(pageSource).toContain('v-if="selectedVisualStyleLabel"')
  })
})
