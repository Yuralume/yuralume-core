import { describe, expect, it } from 'vitest'
import {
  buildDramaTranscript,
  dramaTranscriptFilename,
  type DramaTranscriptBeat,
  type DramaTranscriptStrings,
} from '../src/utils/dramaTranscript'

const strings: DramaTranscriptStrings = {
  you: '你',
  beatFallback: (n) => `第 ${n} 幕`,
}

function beats(): DramaTranscriptBeat[] {
  return [
    {
      title: 'Opening',
      tone: null,
      narration: '霧散開了。',
      playerInput: '',
      exchanges: [
        { playerInput: '我先開口。', response: '她抬起頭看向聲音的方向。' },
      ],
    },
    {
      title: 'The Cut Wire',
      tone: 'dark',
      narration: '她把那截電線舉到燈下。',
      playerInput: '我們走深一點。',
      exchanges: [],
    },
  ]
}

describe('buildDramaTranscript (txt)', () => {
  it('renders narration blocks and player lines in play order', () => {
    const out = buildDramaTranscript({
      title: 'Glass Signal',
      beats: beats(),
      format: 'txt',
      strings,
    })

    expect(out.split('\n\n')).toEqual([
      'Glass Signal',
      '── 1. Opening ──',
      '霧散開了。',
      '你：我先開口。',
      '她抬起頭看向聲音的方向。',
      '── 2. The Cut Wire（dark） ──',
      // The input that chose this branch comes before what it produced.
      '你：我們走深一點。',
      '她把那截電線舉到燈下。\n',
    ])
  })

  it('falls back to a numbered heading when the node title is unknown', () => {
    const out = buildDramaTranscript({
      title: '',
      beats: [{ narration: '燈滅了。' }],
      format: 'txt',
      strings,
    })

    expect(out).toContain('── 1. 第 1 幕 ──')
    // No title line was emitted at all — not an empty one.
    expect(out.startsWith('──')).toBe(true)
  })

  it('keeps a silent beat so the numbering after it stays true', () => {
    const out = buildDramaTranscript({
      title: 'T',
      beats: [{ title: 'A', narration: '   ' }, { title: 'B', narration: 'x' }],
      format: 'txt',
      strings,
    })

    expect(out).toContain('── 1. A ──')
    expect(out).toContain('── 2. B ──')
  })
})

describe('buildDramaTranscript (md)', () => {
  it('carries the beat hierarchy and quotes the player lines', () => {
    const out = buildDramaTranscript({
      title: 'Glass Signal',
      beats: beats(),
      format: 'md',
      strings,
    })

    expect(out).toContain('# Glass Signal')
    expect(out).toContain('## 1. Opening')
    expect(out).toContain('## 2. The Cut Wire（dark）')
    expect(out).toContain('> **你：**我先開口。')
    expect(out).toContain('她抬起頭看向聲音的方向。')
  })

  it('marks every paragraph of a multi-line player line', () => {
    const out = buildDramaTranscript({
      title: 'T',
      beats: [{ narration: 'n', playerInput: '第一句\n第二句' }],
      format: 'md',
      strings,
    })

    // Every line carries the marker, separated by a quoted blank line —
    // Markdown collapses a bare single newline, which would silently glue
    // two things the player typed apart into one sentence.
    expect(out).toContain('> **你：**第一句\n> \n> 第二句')
  })
})

describe('dramaTranscriptFilename', () => {
  it('keeps CJK titles and strips path-illegal characters', () => {
    expect(dramaTranscriptFilename('玻璃/訊號: 上', 'md')).toBe('玻璃 訊號 上.md')
  })

  it('falls back rather than producing a nameless file', () => {
    expect(dramaTranscriptFilename('   ', 'txt')).toBe('drama.txt')
    expect(dramaTranscriptFilename('...', 'txt')).toBe('drama.txt')
  })
})
