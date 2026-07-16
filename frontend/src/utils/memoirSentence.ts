/**
 * Memoir summary splitting helpers.
 *
 * The memoir "book page" renders a free-text summary as an optional
 * bolded title + body paragraphs + an italic trailing parenthetical
 * ("emotional coda"). The original regexes only recognised CJK sentence
 * terminators (。！？) and CJK/ASCII parentheses, so English and mixed
 * summaries never split into a title and rendered as one block. These
 * helpers accept both full-width (CJK) and half-width (Latin) sentence
 * punctuation and measure length by display width so a Latin title is
 * judged on a comparable scale to a CJK one.
 *
 * Pure string utilities — no i18n, no DOM — so they unit-test cleanly.
 */

/** Sentence terminators: CJK full-width + Latin half-width. */
const SENTENCE_TERMINATORS = /^([\s\S]+?)(?:[。！？.!?]|\n)/

/**
 * Trailing parenthetical coda: a `(...)` / `（...）` group at the very
 * end of the text. Accepts both bracket families and requires the inner
 * content to be non-trivial (handled by the caller's length gate).
 */
const TRAILING_CODA = /[（(]([^（()）]*?)[）)]\s*$/

/**
 * Rough display width: CJK / full-width code points count as 2, Latin
 * and other half-width as 1. Lets a single length gate treat "12 個全形字"
 * and a Latin sentence on a comparable scale without a separate branch.
 */
export function displayWidth(text: string): number {
  let width = 0
  for (const ch of text) {
    const code = ch.codePointAt(0) ?? 0
    const isWide =
      (code >= 0x1100 && code <= 0x115f) // Hangul Jamo
      || (code >= 0x2e80 && code <= 0xa4cf) // CJK radicals … Yi
      || (code >= 0xac00 && code <= 0xd7a3) // Hangul syllables
      || (code >= 0xf900 && code <= 0xfaff) // CJK compat ideographs
      || (code >= 0xfe30 && code <= 0xfe4f) // CJK compat forms
      || (code >= 0xff00 && code <= 0xff60) // full-width forms
      || (code >= 0xffe0 && code <= 0xffe6) // full-width signs
      || (code >= 0x20000 && code <= 0x3fffd) // CJK ext B+
    width += isWide ? 2 : 1
  }
  return width
}

export interface MemoirSummaryParts {
  title: string
  paragraphs: string[]
  coda: string
}

export interface SplitOptions {
  /**
   * Max display width for the first sentence to be promoted to a title.
   * Defaults to 48 (≈ 24 full-width chars, matching the legacy gate).
   */
  maxTitleWidth?: number
  /** Min display width so a stray fragment isn't promoted. Defaults 4. */
  minTitleWidth?: number
}

/**
 * Split a memoir summary into { title, paragraphs, coda }.
 *
 * - Trailing `(...)` / `（...）` (length ≥ 8 chars incl. brackets) is
 *   pulled out as the italic coda.
 * - The first sentence (up to a CJK or Latin terminator, or a newline)
 *   is promoted to a title only when its display width sits within the
 *   title gate; otherwise the whole rest is body.
 * - Body is split into paragraphs on blank lines.
 */
export function splitMemoirSummary(
  raw: string,
  options: SplitOptions = {},
): MemoirSummaryParts {
  const text = raw?.trim() ?? ''
  if (!text) return { title: '', paragraphs: [], coda: '' }

  const maxTitleWidth = options.maxTitleWidth ?? 48
  const minTitleWidth = options.minTitleWidth ?? 4

  let coda = ''
  let rest = text
  const codaMatch = rest.match(TRAILING_CODA)
  if (codaMatch && codaMatch[0].length >= 8) {
    coda = codaMatch[0].trim()
    rest = rest.slice(0, rest.length - codaMatch[0].length).trim()
  }

  let title = ''
  let body = rest
  const sentenceMatch = rest.match(SENTENCE_TERMINATORS)
  if (sentenceMatch) {
    const candidate = sentenceMatch[1].trim()
    const width = displayWidth(candidate)
    if (width <= maxTitleWidth && width >= minTitleWidth) {
      title = candidate
      body = rest.slice(sentenceMatch[0].length).trim()
    }
  }

  const paragraphs = body
    .split(/\n+/)
    .map(s => s.trim())
    .filter(Boolean)

  return { title, paragraphs, coda }
}
