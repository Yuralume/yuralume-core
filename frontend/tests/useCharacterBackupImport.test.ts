import { beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  CharacterBackupImportPreview,
  CharacterBackupJob,
} from '@/utils/api/characterBackups'

/**
 * The import half of the backup/restore panel (CB4): stage → unlock/preview
 * → confirm → restore job → new character. The password prompt is reused
 * across a retry (`wrongPassword`), so what matters most is that phase
 * transitions land the player back on the *right* screen for each refusal —
 * a retypeable password stays on the prompt, everything else surfaces
 * elsewhere and the flow resets to "pick a file".
 */
vi.mock('@/utils/api/characterBackups', () => ({
  uploadCharacterBackupFile: vi.fn(),
  previewCharacterBackupImport: vi.fn(),
  startCharacterBackupImport: vi.fn(),
  getCharacterBackupImportJob: vi.fn(),
}))

const {
  getCharacterBackupImportJob,
  previewCharacterBackupImport,
  startCharacterBackupImport,
  uploadCharacterBackupFile,
} = await import('@/utils/api/characterBackups')
const { useCharacterBackupImport } = await import('@/composables/useCharacterBackupImport')

const mockedUpload = vi.mocked(uploadCharacterBackupFile)
const mockedPreview = vi.mocked(previewCharacterBackupImport)
const mockedConfirm = vi.mocked(startCharacterBackupImport)
const mockedPoll = vi.mocked(getCharacterBackupImportJob)

function file(name = 'aria.lumebackup'): File {
  return new File(['x'], name)
}

function preview(overrides: Partial<CharacterBackupImportPreview> = {}): CharacterBackupImportPreview {
  return {
    staged_object_key: 'staged-1',
    character_name: 'Aria',
    original_character_id: 'char-orig',
    schema_version: 1,
    app_version: '1.0.0',
    exported_at: '2026-08-01T00:00:00Z',
    table_counts: { messages: 10 },
    total_rows: 10,
    media_count: 2,
    media_total_bytes: 2048,
    nsfw_collapse: {
      messages_replaced: 0,
      messages_dropped: 0,
      memories_skipped: 0,
      operator_profile_fields_skipped: 0,
      pending_follow_ups_skipped: 0,
    },
    will_collapse_nsfw: false,
    export_report: { skipped_media_keys: [], notes: [] },
    ...overrides,
  }
}

function job(overrides: Partial<CharacterBackupJob> = {}): CharacterBackupJob {
  return {
    job_id: 'restore-1',
    kind: 'restore',
    character_id: '',
    status: 'running',
    stage: 'queued',
    error: null,
    download_ready: false,
    report: null,
    created_at: '2026-08-05T00:00:00Z',
    finished_at: null,
    ...overrides,
  }
}

function axiosError(status: number, detail?: string): unknown {
  return { isAxiosError: true, response: { status, data: { detail } } }
}

async function flushMicrotasks(times = 10): Promise<void> {
  for (let i = 0; i < times; i += 1) await Promise.resolve()
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('staging an upload', () => {
  it('opens the password prompt once the file is staged', async () => {
    mockedUpload.mockResolvedValueOnce({ staged_object_key: 'staged-1', size_bytes: 1024 })
    const importCtl = useCharacterBackupImport()

    await importCtl.selectFile(file())

    expect(importCtl.phase.value).toBe('passwordPrompt')
    expect(importCtl.passwordModalOpen.value).toBe(true)
    expect(importCtl.fileName.value).toBe('aria.lumebackup')
  })

  it('lands on the idle/failed screen with a classified refusal', async () => {
    mockedUpload.mockRejectedValueOnce(axiosError(413))
    const importCtl = useCharacterBackupImport()

    await importCtl.selectFile(file())

    expect(importCtl.phase.value).toBe('failed')
    expect(importCtl.errorKey.value).toBe('tooLarge')
    expect(importCtl.passwordModalOpen.value).toBe(false)
  })
})

describe('unlocking with a password', () => {
  async function staged(): Promise<ReturnType<typeof useCharacterBackupImport>> {
    mockedUpload.mockResolvedValueOnce({ staged_object_key: 'staged-1', size_bytes: 1024 })
    const importCtl = useCharacterBackupImport()
    await importCtl.selectFile(file())
    return importCtl
  }

  it('shows the preview and keeps the password for confirm', async () => {
    const importCtl = await staged()
    mockedPreview.mockResolvedValueOnce(preview())

    await importCtl.submitPassword('correct horse')

    expect(mockedPreview).toHaveBeenCalledWith('staged-1', 'correct horse')
    expect(importCtl.phase.value).toBe('previewReady')
    expect(importCtl.preview.value?.character_name).toBe('Aria')
    expect(importCtl.passwordModalOpen.value).toBe(false)
  })

  it('keeps the prompt open on a wrong password so the upload is not wasted', async () => {
    const importCtl = await staged()
    mockedPreview.mockRejectedValueOnce(axiosError(400, 'Wrong backup password'))

    await importCtl.submitPassword('nope')

    expect(importCtl.phase.value).toBe('passwordPrompt')
    expect(importCtl.passwordModalOpen.value).toBe(true)
    expect(importCtl.errorKey.value).toBe('wrongPassword')
  })

  it('closes the prompt and fails the flow on a corrupt file', async () => {
    const importCtl = await staged()
    mockedPreview.mockRejectedValueOnce(
      axiosError(400, 'Backup file is unreadable, corrupted or tampered with'),
    )

    await importCtl.submitPassword('correct horse')

    expect(importCtl.phase.value).toBe('failed')
    expect(importCtl.passwordModalOpen.value).toBe(false)
    expect(importCtl.errorKey.value).toBe('corruptFile')
  })
})

describe('confirming into a restore job', () => {
  async function readyToConfirm(): Promise<ReturnType<typeof useCharacterBackupImport>> {
    mockedUpload.mockResolvedValueOnce({ staged_object_key: 'staged-1', size_bytes: 1024 })
    mockedPreview.mockResolvedValueOnce(preview())
    const importCtl = useCharacterBackupImport()
    await importCtl.selectFile(file())
    await importCtl.submitPassword('correct horse')
    return importCtl
  }

  it('follows the job through to a new character', async () => {
    const importCtl = await readyToConfirm()
    mockedConfirm.mockResolvedValueOnce(job())
    mockedPoll.mockResolvedValueOnce(job({
      status: 'succeeded',
      stage: 'done',
      character_id: 'char-new',
      report: {
        nsfw_messages_replaced: 1,
        nsfw_messages_dropped: 0,
        nsfw_memories_skipped: 0,
        nsfw_operator_profile_fields_skipped: 0,
        nsfw_pending_follow_ups_skipped: 0,
        media_transferred: 2,
        media_missing: [],
        rows_landed: {},
        rows_skipped: {},
        landed_arc_template_ids: [],
        landed_arc_series_ids: [],
        seed_refs_cleared: 0,
        embedding: { status: 'done', content_vectors: 4, tag_vectors: 4 },
      },
    }))

    await importCtl.confirm()

    expect(mockedConfirm).toHaveBeenCalledWith('staged-1', 'correct horse')
    expect(importCtl.phase.value).toBe('succeeded')
    expect(importCtl.job.value?.character_id).toBe('char-new')
    expect(importCtl.job.value?.report?.media_transferred).toBe(2)
  })

  it('records the terminal failure report when the restore itself fails', async () => {
    const importCtl = await readyToConfirm()
    mockedConfirm.mockResolvedValueOnce(job())
    mockedPoll.mockResolvedValueOnce(job({ status: 'failed', stage: 'failed', error: 'disk full' }))

    await importCtl.confirm()

    expect(importCtl.phase.value).toBe('failed')
    expect(importCtl.errorKey.value).toBe('generic')
    expect(importCtl.errorDetail.value).toBe('disk full')
  })

  it('re-opens the password prompt if confirm itself is refused as a wrong password', async () => {
    const importCtl = await readyToConfirm()
    mockedConfirm.mockRejectedValueOnce(axiosError(400, 'Wrong backup password'))

    await importCtl.confirm()

    expect(importCtl.phase.value).toBe('passwordPrompt')
    expect(importCtl.passwordModalOpen.value).toBe(true)
  })

  it('surfaces the character-create gate with the raw slot-cap reason', async () => {
    const importCtl = await readyToConfirm()
    mockedConfirm.mockRejectedValueOnce(
      axiosError(400, 'You have reached your character slot limit'),
    )

    await importCtl.confirm()

    expect(importCtl.phase.value).toBe('failed')
    expect(importCtl.errorKey.value).toBe('createGate')
    expect(importCtl.errorDetail.value).toBe('You have reached your character slot limit')
  })

  it('refuses to confirm before a preview has unlocked a password', async () => {
    const importCtl = useCharacterBackupImport()

    await importCtl.confirm()

    expect(mockedConfirm).not.toHaveBeenCalled()
  })
})

describe('restarting the flow', () => {
  it('never lets a stale preview land after the player picked a different file', async () => {
    mockedUpload.mockResolvedValueOnce({ staged_object_key: 'staged-1', size_bytes: 1024 })
    const importCtl = useCharacterBackupImport()
    await importCtl.selectFile(file('first.lumebackup'))

    const gate: { resolve?: (p: CharacterBackupImportPreview) => void } = {}
    mockedPreview.mockImplementationOnce(() => new Promise((resolve) => { gate.resolve = resolve }))
    const stalePreview = importCtl.submitPassword('pw-1')
    await flushMicrotasks()

    mockedUpload.mockResolvedValueOnce({ staged_object_key: 'staged-2', size_bytes: 512 })
    await importCtl.selectFile(file('second.lumebackup'))
    gate.resolve?.(preview({ staged_object_key: 'staged-1', character_name: 'Stale' }))
    await stalePreview

    expect(importCtl.phase.value).toBe('passwordPrompt')
    expect(importCtl.fileName.value).toBe('second.lumebackup')
    expect(importCtl.preview.value).toBeNull()
  })

  it('reset() clears the password and every phase-scoped field', async () => {
    mockedUpload.mockResolvedValueOnce({ staged_object_key: 'staged-1', size_bytes: 1024 })
    mockedPreview.mockResolvedValueOnce(preview())
    const importCtl = useCharacterBackupImport()
    await importCtl.selectFile(file())
    await importCtl.submitPassword('correct horse')

    importCtl.reset()

    expect(importCtl.phase.value).toBe('idle')
    expect(importCtl.preview.value).toBeNull()
    expect(importCtl.fileName.value).toBeNull()
    expect(importCtl.job.value).toBeNull()
    expect(importCtl.errorKey.value).toBeNull()
  })
})
