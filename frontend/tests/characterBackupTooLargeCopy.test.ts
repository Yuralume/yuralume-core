import { describe, expect, it } from 'vitest'

import { messages as zhTW } from '@/i18n/locales/zh-TW'
import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'

/**
 * FE-4: `characterBackup.errors.tooLarge` used to hardcode "2 GiB" in all
 * three locales, but the backend limit
 * (`character_backup_import_service.py`'s `BACKUP_IMPORT_MAX_BYTES`) is an
 * owner-tunable constant — and the *other* 413 source on this same surface
 * (the archive unpack budget, `DEFAULT_MAX_UNCOMPRESSED_BYTES`) is already a
 * different number (4 GiB) sharing this one frontend key. Pinning a literal
 * number here is wrong on day one for one of the two 413s and stale the
 * moment an owner retunes the other, so the copy must not spell out a size
 * at all. This guards the regression rather than a specific replacement
 * string — restating the fix pins nothing.
 */
const CATALOGS = { 'zh-TW': zhTW, 'en-US': enUS, 'ja-JP': jaJP }

const BYTE_SIZE_LITERAL = /\d[\d.,]*\s*(?:[KMGT]i?B)\b/i

describe('characterBackup.errors.tooLarge copy', () => {
  it.each(Object.entries(CATALOGS))(
    'does not hardcode a byte-size literal in %s',
    (_locale, catalog) => {
      const copy = catalog.characterBackup.errors.tooLarge
      expect(copy).toBeTruthy()
      expect(copy).not.toMatch(BYTE_SIZE_LITERAL)
    },
  )
})
