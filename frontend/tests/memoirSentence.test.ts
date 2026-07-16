import { describe, expect, it } from 'vitest'

import { displayWidth, splitMemoirSummary } from '@/utils/memoirSentence'

describe('splitMemoirSummary', () => {
  it('promotes a short CJK first sentence to a title', () => {
    const out = splitMemoirSummary('雨天的午後。我在咖啡店寫了很久的字。')
    expect(out.title).toBe('雨天的午後')
    expect(out.paragraphs).toEqual(['我在咖啡店寫了很久的字。'])
  })

  it('splits an English summary on a Latin period (previously unsupported)', () => {
    const out = splitMemoirSummary('A quiet afternoon. I wrote for a long time at the cafe.')
    expect(out.title).toBe('A quiet afternoon')
    expect(out.paragraphs).toEqual(['I wrote for a long time at the cafe.'])
  })

  it('splits a Japanese summary on a full-width period', () => {
    const out = splitMemoirSummary('静かな午後。カフェで長い間書いていた。')
    expect(out.title).toBe('静かな午後')
    expect(out.paragraphs).toEqual(['カフェで長い間書いていた。'])
  })

  it('extracts a trailing parenthetical coda in either bracket family', () => {
    const cjk = splitMemoirSummary('今天很累。（情緒尾韻：被同事煩到頭痛）')
    expect(cjk.coda).toBe('（情緒尾韻：被同事煩到頭痛）')
    const latin = splitMemoirSummary('Tired today. (emotional residue: a nagging headache)')
    expect(latin.coda).toBe('(emotional residue: a nagging headache)')
  })

  it('leaves an over-long first sentence in the body (no title promotion)', () => {
    const long = 'This is a very long opening sentence that keeps going well past any reasonable title length so it should not be promoted. And here is more.'
    const out = splitMemoirSummary(long)
    expect(out.title).toBe('')
    expect(out.paragraphs.length).toBeGreaterThan(0)
  })

  it('handles empty / whitespace input', () => {
    expect(splitMemoirSummary('')).toEqual({ title: '', paragraphs: [], coda: '' })
    expect(splitMemoirSummary('   ')).toEqual({ title: '', paragraphs: [], coda: '' })
  })

  it('splits body paragraphs on blank lines', () => {
    const out = splitMemoirSummary('標題。\n第一段\n\n第二段')
    expect(out.title).toBe('標題')
    expect(out.paragraphs).toEqual(['第一段', '第二段'])
  })
})

describe('displayWidth', () => {
  it('counts CJK as double width and Latin as single', () => {
    expect(displayWidth('abcd')).toBe(4)
    expect(displayWidth('中文')).toBe(4)
    expect(displayWidth('a中')).toBe(3)
  })
})
