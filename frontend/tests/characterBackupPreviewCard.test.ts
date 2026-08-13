import { describe, expect, it } from 'vitest'
import { createSSRApp } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createI18n } from 'vue-i18n'

import CharacterBackupImportPreviewCard from '@/components/CharacterBackupImportPreviewCard.vue'
import { messages as zhTW } from '@/i18n/locales/zh-TW'
import type {
  CharacterBackupImportPreview,
  CharacterBackupNsfwCollapseStats,
} from '@/utils/api/characterBackups'

function collapse(
  overrides: Partial<CharacterBackupNsfwCollapseStats> = {},
): CharacterBackupNsfwCollapseStats {
  return {
    messages_replaced: 0,
    messages_dropped: 0,
    memories_skipped: 0,
    operator_profile_fields_skipped: 0,
    pending_follow_ups_skipped: 0,
    ...overrides,
  }
}

// No DOM test infra exists in this repo (see `fusionStoryExitHub.test.ts`),
// so this uses the same SSR-render technique: `renderToString` runs setup()
// + template, enough to assert the NSFW-fold callout — the one thing D5
// insists the player cannot miss before confirming — actually renders, and
// stays gone when nothing about this import would collapse.

function i18n() {
  return createI18n({
    legacy: false,
    locale: 'zh-TW',
    fallbackLocale: 'zh-TW',
    messages: { 'zh-TW': zhTW },
  })
}

async function render(preview: CharacterBackupImportPreview): Promise<string> {
  const app = createSSRApp(CharacterBackupImportPreviewCard, { preview })
  app.use(i18n())
  return renderToString(app)
}

const L = zhTW.characterBackup.preview

function basePreview(overrides: Partial<CharacterBackupImportPreview> = {}): CharacterBackupImportPreview {
  return {
    staged_object_key: 'staged-1',
    character_name: 'Aria',
    original_character_id: 'char-orig',
    schema_version: 1,
    app_version: '1.0.0',
    exported_at: '2026-08-01T00:00:00Z',
    table_counts: { messages: 42 },
    total_rows: 42,
    media_count: 3,
    media_total_bytes: 5 * 1024 * 1024,
    nsfw_collapse: collapse(),
    will_collapse_nsfw: false,
    export_report: { skipped_media_keys: [], notes: [] },
    ...overrides,
  }
}

describe('CharacterBackupImportPreviewCard (SSR render)', () => {
  it('shows the character name, row count, and media size', async () => {
    const html = await render(basePreview())
    expect(html).toContain('Aria')
    expect(html).toContain('42')
    expect(html).toContain('5.0 MB')
  })

  it('hides the NSFW-fold notice when nothing would be folded', async () => {
    const html = await render(basePreview({ will_collapse_nsfw: false }))
    expect(html).not.toContain(L.nsfwLead)
  })

  it('hides the notice even when the flag is set but every count is zero', async () => {
    // A hosted deployment with nothing NSFW in this particular backup —
    // the flag says "this deployment would collapse", not "this file has
    // anything to collapse".
    const html = await render(basePreview({
      will_collapse_nsfw: true,
      nsfw_collapse: collapse(),
    }))
    expect(html).not.toContain(L.nsfwLead)
  })

  it('surfaces all fold numbers before the player can confirm', async () => {
    const html = await render(basePreview({
      will_collapse_nsfw: true,
      nsfw_collapse: collapse({
        messages_replaced: 5, messages_dropped: 2, memories_skipped: 1,
      }),
    }))
    expect(html).toContain(L.nsfwLead)
    expect(html).toContain('5')
    expect(html).toContain('2')
    // The 1-memory count line renders too (share the digit with the count
    // above, so check the whole i18n line is present as its own string).
    expect(html).toContain(L.nsfwMemoriesSkipped.replace('{count}', '1'))
  })

  it('surfaces the profile-field and follow-up carriers too (D5)', async () => {
    // The fold is not just messages/memories: an nsfw operator-profile
    // field and an nsfw pending-follow-up are their own at-rest carriers,
    // and the player must be told they will be skipped before confirming.
    const html = await render(basePreview({
      will_collapse_nsfw: true,
      nsfw_collapse: collapse({
        operator_profile_fields_skipped: 4,
        pending_follow_ups_skipped: 7,
      }),
    }))
    expect(html).toContain(L.nsfwLead)
    expect(html).toContain(L.nsfwProfileFieldsSkipped.replace('{count}', '4'))
    expect(html).toContain(L.nsfwPendingFollowUpsSkipped.replace('{count}', '7'))
  })

  it('only mentions the fields that actually collapsed', async () => {
    const html = await render(basePreview({
      will_collapse_nsfw: true,
      nsfw_collapse: collapse({ messages_replaced: 3 }),
    }))
    expect(html).toContain(L.nsfwReplaced.replace('{count}', '3'))
    expect(html).not.toContain(L.nsfwDropped.replace('{count}', '0'))
    expect(html).not.toContain(L.nsfwMemoriesSkipped.replace('{count}', '0'))
    expect(html).not.toContain(
      L.nsfwProfileFieldsSkipped.replace('{count}', '0'),
    )
  })

  it('mentions media that already failed to read at export time', async () => {
    const html = await render(basePreview({
      export_report: { skipped_media_keys: ['a', 'b'], notes: [] },
    }))
    expect(html).toContain(L.skippedMediaAtExport.replace('{count}', '2'))
  })

  it('says nothing about export-time skips when there were none', async () => {
    const html = await render(basePreview())
    // The whole paragraph is `v-if`-gated on a nonzero count, so check for
    // the message's static lead-in rather than any one interpolated form.
    const [staticLeadIn] = L.skippedMediaAtExport.split('{count}')
    expect(html).not.toContain(staticLeadIn)
  })

  it('offers both confirm and cancel', async () => {
    const html = await render(basePreview())
    expect(html).toContain(L.confirmAction)
    expect(html).toContain(zhTW.common.actions.cancel)
  })
})
