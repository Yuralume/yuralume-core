/**
 * WorldEventFeedsPanel's feed table (F3): a source scan pinning the
 * overflow-x wrap convention shared with CharacterFreezeAdminPage /
 * MemoriesAdminPage. This repo has no jsdom / `@vue/test-utils` (see
 * tests/lightbox.test.ts's header), so a mount-and-measure test isn't
 * available here -- the scan instead checks that the table is wrapped
 * in a `.world-feeds__table-wrap` element carrying `overflow-x: auto`,
 * so a 390px viewport scrolls the table rather than the admin page body.
 */

import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const SOURCE = readFileSync(
  fileURLToPath(new URL('../src/components/admin/WorldEventFeedsPanel.vue', import.meta.url)),
  'utf-8',
)

describe('WorldEventFeedsPanel table overflow', () => {
  it('wraps the feed table in an overflow-x scroll container', () => {
    expect(SOURCE).toMatch(/<div v-else class="world-feeds__table-wrap">\s*<table/)
  })

  it('gives the wrap class overflow-x: auto', () => {
    const rule = SOURCE.match(/\.world-feeds__table-wrap\s*\{[^}]*\}/)?.[0] ?? ''
    expect(rule).toMatch(/overflow-x:\s*auto/)
  })
})
