/**
 * BD7 — compose a played branching-drama path into a readable transcript.
 *
 * Pure and offline on purpose: the playthrough is already fully on the
 * player's screen, so asking the server to render it again would be a
 * round trip (and a bill) for text the browser is holding. Nothing here
 * touches the network or the DOM — the caller owns the download.
 *
 * The two formats say the same thing at different resolutions: `txt` is
 * the script as it reads aloud, `md` adds the beat hierarchy so the file
 * survives being pasted into a document with an outline.
 */

export type DramaTranscriptFormat = 'txt' | 'md'

export interface DramaTranscriptExchange {
  playerInput: string
  response: string
}

export interface DramaTranscriptBeat {
  /** The authored node title, when the client knows it. */
  title?: string
  /** Which tonal branch this beat was, when it was a branch at all. */
  tone?: string | null
  narration: string
  /** What the player said to arrive here — empty for the opening beat. */
  playerInput?: string
  exchanges?: DramaTranscriptExchange[]
}

export interface DramaTranscriptStrings {
  /** How the player is labelled in their own lines — 「你」. */
  you: string
  /** Heading for a beat whose authored title is unknown, e.g. 「第 {n} 幕」. */
  beatFallback: (index: number) => string
}

export interface DramaTranscriptInput {
  title: string
  beats: DramaTranscriptBeat[]
  format: DramaTranscriptFormat
  strings: DramaTranscriptStrings
}

function beatHeading(
  beat: DramaTranscriptBeat,
  index: number,
  strings: DramaTranscriptStrings,
): string {
  const title = (beat.title ?? '').trim()
  const base = `${index + 1}. ${title || strings.beatFallback(index + 1)}`
  const tone = (beat.tone ?? '').trim()
  return tone ? `${base}（${tone}）` : base
}

function block(text: string): string[] {
  const cleaned = (text ?? '').trim()
  return cleaned ? [cleaned] : []
}

function quoteForMarkdown(text: string): string {
  // A player line can be several paragraphs; every one of them has to
  // carry the quote marker or the tail falls out of the blockquote.
  return text
    .trim()
    .split(/\r?\n/)
    .map((line) => `> ${line}`.trimEnd())
    .join('\n> \n')
}

function playerLine(
  text: string,
  format: DramaTranscriptFormat,
  strings: DramaTranscriptStrings,
): string[] {
  const cleaned = (text ?? '').trim()
  if (!cleaned) return []
  if (format === 'md') {
    return [quoteForMarkdown(`**${strings.you}：**${cleaned}`)]
  }
  return [`${strings.you}：${cleaned}`]
}

/**
 * Render one playthrough. Beats keep play order; a beat with no narration
 * and no exchanges still gets its heading, because a silent beat is a
 * thing that happened and dropping it would renumber everything after it.
 */
export function buildDramaTranscript(input: DramaTranscriptInput): string {
  const { title, beats, format, strings } = input
  const parts: string[] = []
  const heading = (title ?? '').trim()
  if (heading) parts.push(format === 'md' ? `# ${heading}` : heading)

  beats.forEach((beat, index) => {
    const label = beatHeading(beat, index, strings)
    parts.push(format === 'md' ? `## ${label}` : `── ${label} ──`)
    parts.push(...playerLine(beat.playerInput ?? '', format, strings))
    parts.push(...block(beat.narration))
    for (const exchange of beat.exchanges ?? []) {
      parts.push(...playerLine(exchange.playerInput, format, strings))
      parts.push(...block(exchange.response))
    }
  })

  return `${parts.join('\n\n')}\n`
}

/**
 * A filename the OS will accept, built from the drama's own title.
 *
 * Only the characters that are actually illegal in a Windows / POSIX path
 * are replaced — CJK titles survive intact, which is the common case here
 * and the whole reason this is not a slugifier.
 */
export function dramaTranscriptFilename(
  title: string,
  format: DramaTranscriptFormat,
): string {
  const cleaned = (title ?? '')
    .replace(/[\/:*?"<>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80)
    // A trailing dot names a file Windows refuses to create.
    .replace(/\.+$/, '')
    .trim()
  return `${cleaned || 'drama'}.${format}`
}
