import { beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Refusal classification for the backup surface (CB4).
 *
 * The backup routes answer plain `{"detail": "<string>"}` (no structured
 * `code`), so what is pinned here is that status-code-only classification
 * stays *contextual*: the same `400` means three different things depending
 * on which call site saw it, and only the exact fixed-string details tell
 * those apart (`character_backups.py`'s module docstring / `_map_import_file_error`).
 */

vi.mock('axios', () => ({
  default: {
    isAxiosError: vi.fn((error: unknown) => (
      Boolean(error && typeof error === 'object' && 'isAxiosError' in error)
    )),
  },
}))

const {
  characterBackupConfirmErrorKey,
  characterBackupDownloadErrorKey,
  characterBackupErrorDetail,
  characterBackupExportStartErrorKey,
  characterBackupFallbackMessage,
  characterBackupPreviewErrorKey,
  characterBackupStatusCode,
  characterBackupUploadErrorKey,
  isBackupWrongPassword,
} = await import('@/utils/characterBackupErrors')

function axiosError(status: number, detail?: string): unknown {
  return {
    isAxiosError: true,
    response: {
      status,
      data: detail === undefined ? {} : { detail },
    },
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('low-level extraction', () => {
  it('reads the status and detail off an axios error', () => {
    const error = axiosError(404, 'gone')
    expect(characterBackupStatusCode(error)).toBe(404)
    expect(characterBackupErrorDetail(error)).toBe('gone')
  })

  it('answers null for anything that is not an axios error', () => {
    expect(characterBackupStatusCode(new Error('offline'))).toBeNull()
    expect(characterBackupErrorDetail(new Error('offline'))).toBeNull()
    expect(characterBackupStatusCode(null)).toBeNull()
  })

  it('treats a blank detail the same as no detail', () => {
    expect(characterBackupErrorDetail(axiosError(400, '   '))).toBeNull()
  })
})

describe('export start (POST .../backup)', () => {
  it.each([
    [409, 'conflict'],
    [429, 'rateLimited'],
    [503, 'unavailable'],
    [404, 'notFound'],
    [500, 'generic'],
  ] as const)('maps %i to %s', (status, expected) => {
    expect(characterBackupExportStartErrorKey(axiosError(status))).toBe(expected)
  })
})

describe('download (GET .../backup/jobs/{id}/download)', () => {
  it.each([
    [404, 'expired'],
    [409, 'notReady'],
    [500, 'generic'],
  ] as const)('maps %i to %s', (status, expected) => {
    expect(characterBackupDownloadErrorKey(axiosError(status))).toBe(expected)
  })
})

describe('upload (POST /characters/backup/upload)', () => {
  it.each([
    [400, 'invalidFile'],
    [413, 'tooLarge'],
    [503, 'unavailable'],
    [500, 'generic'],
  ] as const)('maps %i to %s', (status, expected) => {
    expect(characterBackupUploadErrorKey(axiosError(status))).toBe(expected)
  })
})

describe('preview (POST /characters/backup/preview)', () => {
  it('tells the wrong password apart from every other 400 by exact text', () => {
    expect(isBackupWrongPassword(axiosError(400, 'Wrong backup password'))).toBe(true)
    expect(characterBackupPreviewErrorKey(axiosError(400, 'Wrong backup password')))
      .toBe('wrongPassword')
  })

  it('reads any other 400 as a corrupt/tampered file (create gates never run at preview)', () => {
    expect(characterBackupPreviewErrorKey(
      axiosError(400, 'Backup file is unreadable, corrupted or tampered with'),
    )).toBe('corruptFile')
    // Even an unrecognized 400 body — preview has no third meaning to land on.
    expect(characterBackupPreviewErrorKey(axiosError(400, 'something else'))).toBe('corruptFile')
  })

  it.each([
    [404, 'expired'],
    [422, 'schemaTooNew'],
    [413, 'tooLarge'],
    [500, 'generic'],
  ] as const)('maps %i to %s', (status, expected) => {
    expect(characterBackupPreviewErrorKey(axiosError(status))).toBe(expected)
  })
})

describe('confirm (POST /characters/backup/import)', () => {
  it('keeps the wrong-password and corrupt-file grammar exact', () => {
    expect(characterBackupConfirmErrorKey(axiosError(400, 'Wrong backup password')))
      .toBe('wrongPassword')
    expect(characterBackupConfirmErrorKey(
      axiosError(400, 'Backup file is unreadable, corrupted or tampered with'),
    )).toBe('corruptFile')
  })

  it('reads every other 400 as the character-create gate (slot cap / daily limit)', () => {
    // This is the one status confirm and preview disagree about: confirm
    // runs the create gates, preview never does.
    expect(characterBackupConfirmErrorKey(
      axiosError(400, 'You have reached your character slot limit'),
    )).toBe('createGate')
  })

  it.each([
    [404, 'expired'],
    [422, 'schemaTooNew'],
    [413, 'tooLarge'],
    [500, 'generic'],
  ] as const)('maps %i to %s', (status, expected) => {
    expect(characterBackupConfirmErrorKey(axiosError(status))).toBe(expected)
  })
})

describe('fallback message', () => {
  it('prefers the raw server detail', () => {
    expect(characterBackupFallbackMessage(axiosError(400, 'slot cap reached')))
      .toBe('slot cap reached')
  })

  it('falls back to Error#message when there is no axios detail', () => {
    expect(characterBackupFallbackMessage(new Error('boom'))).toBe('boom')
  })

  it('gives up and returns null for anything else', () => {
    expect(characterBackupFallbackMessage('not an error')).toBeNull()
  })
})
