/**
 * The create form's art-direction picker, as a state machine (BD10).
 *
 * The picker has to do two things that pull against each other: follow the
 * cast (picking an anime lead should pre-select anime, and changing the
 * lead should move it) while never overruling a player who answered the
 * question themselves. One boolean settles it — once the player touches
 * the picker, the cast stops driving it, for the rest of that form.
 *
 * Extracted from the page because that is the whole rule worth testing: a
 * `watch` that quietly clobbers a deliberate choice looks identical to one
 * that does not, until someone picks realistic, adds a second character,
 * and gets an anime drama they paid for.
 */

import {
  DEFAULT_DRAMA_VISUAL_STYLE,
  type DramaVisualStyle,
} from '@/types/branchingDrama'

/** The shape of a character this module needs — nothing more. */
export interface StyledCharacter {
  id: string
  /** `''` means "inherit", which is not a style this picker can show. */
  visual_generation_style?: string | null
}

export interface VisualStyleSelection {
  style: DramaVisualStyle
  /**
   * Has the player answered the question themselves? While `false` the
   * selection is only a suggestion and follows the cast; once `true` the
   * cast never moves it again.
   */
  touched: boolean
}

function isDramaVisualStyle(value: unknown): value is DramaVisualStyle {
  return value === 'anime' || value === 'realistic'
}

/**
 * What the picker should suggest for a given cast: the *first* selected
 * character's own style.
 *
 * Deliberately the first and only the first, mirroring the backend's
 * `_resolve_visual_style`. A character set to inherit (`''`) has no style
 * of its own to show, and the browser cannot see the owner's stored
 * preference the backend would consult next — so the product default
 * stands in. Note the form always submits whatever is on screen
 * (WYSIWYG — what the player sees is what gets stored); the backend's
 * own resolution chain (first character → owner preference → default)
 * only runs for clients that omit the field entirely, such as the
 * fusion "換個玩法" handoff or pre-BD10 clients.
 */
export function defaultVisualStyleForCast(
  castIds: readonly string[],
  characters: readonly StyledCharacter[],
): DramaVisualStyle {
  const firstId = castIds[0]
  if (!firstId) return DEFAULT_DRAMA_VISUAL_STYLE
  const first = characters.find((c) => c.id === firstId)
  const style = first?.visual_generation_style
  return isDramaVisualStyle(style) ? style : DEFAULT_DRAMA_VISUAL_STYLE
}

/** A fresh, untouched picker — what the form opens on and resets to. */
export function initialVisualStyleSelection(): VisualStyleSelection {
  return { style: DEFAULT_DRAMA_VISUAL_STYLE, touched: false }
}

/** The player answered the question; the cast stops driving the picker. */
export function chooseVisualStyle(
  style: DramaVisualStyle,
): VisualStyleSelection {
  return { style, touched: true }
}

/**
 * Re-derive the suggestion after the cast (or the loaded character list)
 * changed. A touched selection is returned untouched — identity included,
 * so a `watch` writing this back cannot loop.
 */
export function syncVisualStyleWithCast(
  selection: VisualStyleSelection,
  castIds: readonly string[],
  characters: readonly StyledCharacter[],
): VisualStyleSelection {
  if (selection.touched) return selection
  const style = defaultVisualStyleForCast(castIds, characters)
  if (style === selection.style) return selection
  return { style, touched: false }
}
