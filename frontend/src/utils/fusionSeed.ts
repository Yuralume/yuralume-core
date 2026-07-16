/**
 * Pure helpers that turn a *finished* fusion story into the seed of the
 * next creation (Creator Studio C1 "exit hub"). Kept framework-free and
 * i18n-agnostic: every user-visible label is injected by the caller
 * (already run through `t()`), so this module never hard-codes prose and
 * stays trivially unit-testable.
 *
 * Consumers share this seam:
 *   - "續寫本篇" (continue) → `composeContinuationSeed`
 *   - "換個玩法" (branch) → `serializeCastQuery` for the router query;
 *     `parseCastQuery` on the receiving page filters back to owned ids.
 *   - "寫成番外" (C1-P3 memory / chat entry points) → `composeMomentSeed`
 *     turns a chat message or memory summary into a starting-point seed.
 * The parse/serialize pair is also the shared接點 for C1-P3 (memory
 * entry points): any surface that hands a cast + prompt to a creator
 * form goes through these.
 */

/** Hard ceiling for the create-form prompt field (backend accepts ≤2000). */
export const MAX_SEED_PROMPT = 2000

/** Continuation seeds stay compact so the prompt box reads as guidance,
 *  not a wall of text. */
export const MAX_CONTINUATION_SEED = 1500

/** Moment seeds (chat message / memory entry point) stay compact for the
 *  same reason as continuation seeds. */
export const MAX_MOMENT_SEED = 1500

const BLOCK_SEPARATOR = '\n\n'

export interface ContinuationSeedStrings {
  /** e.g. localized "前情提要" */
  recapLabel: string
  /** e.g. localized "結局片段" */
  endingLabel: string
  /** e.g. localized "續寫指示" */
  instructionLabel: string
  /** The actual continuation instruction sentence, localized. */
  instruction: string
}

export interface ContinuationSeedInput {
  title: string
  premise: string
  /** The completed story's full text; only its tail is carried over. */
  endingText: string
  strings: ContinuationSeedStrings
}

function asText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

/**
 * Compose a "recap + ending tail + continuation instruction" seed from a
 * finished story, bounded to {@link MAX_CONTINUATION_SEED}. The ending is
 * taken from the *tail* (the reader just finished it, and we want the
 * model to pick up where it left off rather than re-open the story).
 */
export function composeContinuationSeed(input: ContinuationSeedInput): string {
  const { strings } = input
  const title = asText(input.title)
  const premise = asText(input.premise)
  const ending = asText(input.endingText)

  const recap = [strings.recapLabel, title, premise].filter(Boolean).join('\n')
  const instruction = [strings.instructionLabel, strings.instruction]
    .filter(Boolean)
    .join('\n')

  // Reserve room for the fixed blocks + separators before deciding how
  // much of the ending tail fits.
  const fixedLength =
    recap.length +
    instruction.length +
    strings.endingLabel.length +
    BLOCK_SEPARATOR.length * 2 +
    1 // endingLabel → tail newline
  const endingBudget = MAX_CONTINUATION_SEED - fixedLength

  const blocks: string[] = []
  if (recap) blocks.push(recap)
  if (ending && endingBudget > 0) {
    const tail =
      ending.length > endingBudget ? ending.slice(-endingBudget) : ending
    blocks.push([strings.endingLabel, tail].filter(Boolean).join('\n'))
  }
  if (instruction) blocks.push(instruction)

  const seed = blocks.join(BLOCK_SEPARATOR)
  return seed.length > MAX_CONTINUATION_SEED
    ? seed.slice(0, MAX_CONTINUATION_SEED)
    : seed
}

export interface MomentSeedStrings {
  /** e.g. localized "此刻的片段" — labels the carried memory / message. */
  momentLabel: string
  /** e.g. localized "番外指示" */
  instructionLabel: string
  /** The actual "write a side-story from this moment" instruction. */
  instruction: string
}

export interface MomentSeedInput {
  /** Source text: a chat message's full content or a memory summary. */
  momentText: string
  strings: MomentSeedStrings
}

/**
 * Compose a "moment excerpt + side-story instruction" seed from an
 * existing memory or chat message, bounded to {@link MAX_MOMENT_SEED}.
 *
 * Unlike {@link composeContinuationSeed} — which carries the story's
 * *tail* because the reader just finished it — a moment is the *starting
 * point* of the new story. So when the source is too long we keep its
 * HEAD (the opening of the memory / message) and drop the rest, rather
 * than slicing from the end.
 */
export function composeMomentSeed(input: MomentSeedInput): string {
  const { strings } = input
  const moment = asText(input.momentText)

  const instruction = [strings.instructionLabel, strings.instruction]
    .filter(Boolean)
    .join('\n')

  // Reserve room for the instruction block + the moment label + the
  // separator before deciding how much of the moment HEAD fits.
  const fixedLength =
    instruction.length +
    strings.momentLabel.length +
    BLOCK_SEPARATOR.length +
    1 // momentLabel → head newline
  const momentBudget = MAX_MOMENT_SEED - fixedLength

  const blocks: string[] = []
  if (moment && momentBudget > 0) {
    const head =
      moment.length > momentBudget ? moment.slice(0, momentBudget) : moment
    blocks.push([strings.momentLabel, head].filter(Boolean).join('\n'))
  }
  if (instruction) blocks.push(instruction)

  const seed = blocks.join(BLOCK_SEPARATOR)
  return seed.length > MAX_MOMENT_SEED
    ? seed.slice(0, MAX_MOMENT_SEED)
    : seed
}

/**
 * Parse a comma-separated `cast` query value into an ordered, de-duped
 * list of ids the current viewer actually owns. Anything non-string,
 * empty, unknown, or duplicated is dropped — a query param is untrusted
 * input, so we never seed a form with a character the user can't use.
 */
export function parseCastQuery(raw: unknown, ownedIds: string[]): string[] {
  if (typeof raw !== 'string' || raw.length === 0) return []
  const owned = new Set(ownedIds)
  const seen = new Set<string>()
  const result: string[] = []
  for (const part of raw.split(',')) {
    const id = part.trim()
    if (!id || seen.has(id) || !owned.has(id)) continue
    seen.add(id)
    result.push(id)
  }
  return result
}

/** Serialize a cast id list into a `cast` query value (drops falsy ids). */
export function serializeCastQuery(ids: string[]): string {
  return ids.filter(Boolean).join(',')
}

/** Clamp an arbitrary seed prompt to the create-form's accepted length. */
export function clampSeedPrompt(raw: string): string {
  if (typeof raw !== 'string') return ''
  return raw.length > MAX_SEED_PROMPT ? raw.slice(0, MAX_SEED_PROMPT) : raw
}
