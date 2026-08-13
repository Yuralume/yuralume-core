import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { CharacterBackupJob } from '@/utils/api/characterBackups'

/**
 * The export half of the backup/restore panel (CB4): password → running job
 * → download. What matters most here is the same thing `useStoryScene`
 * pins for its own state machine — a slow or stale answer must never write
 * itself onto a flow the player has since reset or restarted.
 */
vi.mock('@/utils/api/characterBackups', () => ({
  startCharacterBackupExport: vi.fn(),
  getCharacterBackupExportJob: vi.fn(),
  downloadCharacterBackup: vi.fn(),
  probeCharacterBackupDownload: vi.fn(),
}))

const {
  downloadCharacterBackup,
  getCharacterBackupExportJob,
  probeCharacterBackupDownload,
  startCharacterBackupExport,
} = await import('@/utils/api/characterBackups')
const { useCharacterBackupExport } = await import('@/composables/useCharacterBackupExport')

const mockedStart = vi.mocked(startCharacterBackupExport)
const mockedPoll = vi.mocked(getCharacterBackupExportJob)
const mockedDownload = vi.mocked(downloadCharacterBackup)
const mockedProbe = vi.mocked(probeCharacterBackupDownload)

function job(overrides: Partial<CharacterBackupJob> = {}): CharacterBackupJob {
  return {
    job_id: 'job-1',
    kind: 'export',
    character_id: 'char-1',
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

/**
 * Let a gated request's caller walk its own awaits (submit → runPoll →
 * pollBackupJob → fetch) without settling the gate, so a test can act at a
 * point the composable is genuinely at. Mirrors `useStoryScene.test.ts`.
 */
async function flushMicrotasks(times = 10): Promise<void> {
  for (let i = 0; i < times; i += 1) await Promise.resolve()
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('starting an export', () => {
  it('runs the job to completion and offers the download', async () => {
    mockedStart.mockResolvedValueOnce(job({ stage: 'queued' }))
    // Resolves terminal on the very first poll — a second running answer
    // would introduce a real sleep between reads, which the dedicated
    // `characterBackupPolling.test.ts` already covers with fake timers.
    mockedPoll.mockResolvedValueOnce(
      job({ stage: 'upload', status: 'succeeded', download_ready: true }),
    )
    const exportCtl = useCharacterBackupExport()

    await exportCtl.submit('char-1', 'a-very-long-password')

    expect(exportCtl.passwordModalOpen.value).toBe(false)
    expect(exportCtl.job.value?.status).toBe('succeeded')
    expect(exportCtl.job.value?.download_ready).toBe(true)
    expect(exportCtl.errorKey.value).toBeNull()
  })

  it('closes the modal and classifies a start refusal instead of retrying blind', async () => {
    mockedStart.mockRejectedValueOnce(axiosError(409))
    const exportCtl = useCharacterBackupExport()
    exportCtl.openPasswordModal()

    await exportCtl.submit('char-1', 'a-very-long-password')

    expect(exportCtl.passwordModalOpen.value).toBe(false)
    expect(exportCtl.errorKey.value).toBe('conflict')
    expect(exportCtl.job.value).toBeNull()
  })

  it('surfaces a failed terminal job as an error instead of going silent (FE-2)', async () => {
    mockedStart.mockResolvedValueOnce(job({ stage: 'encrypt' }))
    // `pollBackupJob` resolves normally on a `failed` status — it is not a
    // fetch exception, so the old code's catch-only error handling never
    // saw it and the progress bar just vanished with no refusal anywhere.
    mockedPoll.mockResolvedValueOnce(
      job({ stage: 'failed', status: 'failed', error: 'disk full mid-export' }),
    )
    const exportCtl = useCharacterBackupExport()

    await exportCtl.submit('char-1', 'a-very-long-password')

    expect(exportCtl.job.value?.status).toBe('failed')
    expect(exportCtl.errorKey.value).toBe('generic')
    expect(exportCtl.errorDetail.value).toBe('disk full mid-export')
  })

  it('never lets a stale poll from a reset job overwrite the state on screen', async () => {
    mockedStart.mockResolvedValueOnce(job({ job_id: 'job-1' }))
    const gate: { resolve?: (j: CharacterBackupJob) => void } = {}
    mockedPoll.mockImplementationOnce(() => new Promise((resolve) => { gate.resolve = resolve }))
    const exportCtl = useCharacterBackupExport()

    const inFlight = exportCtl.submit('char-1', 'a-very-long-password')
    // Give `submit` enough ticks to reach the poll's pending fetch.
    await flushMicrotasks()
    exportCtl.reset()
    gate.resolve?.(job({ job_id: 'job-1', status: 'succeeded', download_ready: true }))
    await inFlight

    expect(exportCtl.job.value).toBeNull()
  })
})

describe('downloading', () => {
  it('probes first, then triggers the native download', async () => {
    mockedStart.mockResolvedValueOnce(job())
    mockedPoll.mockResolvedValueOnce(job({ status: 'succeeded', download_ready: true }))
    mockedProbe.mockResolvedValueOnce(undefined)
    const exportCtl = useCharacterBackupExport()
    await exportCtl.submit('char-1', 'a-very-long-password')

    await exportCtl.download('char-1')

    expect(mockedProbe).toHaveBeenCalledWith('char-1', 'job-1')
    expect(mockedDownload).toHaveBeenCalledWith('char-1', 'job-1')
  })

  it('drops the job on a TTL-expired probe instead of leaving a dead download button', async () => {
    mockedStart.mockResolvedValueOnce(job())
    mockedPoll.mockResolvedValueOnce(job({ status: 'succeeded', download_ready: true }))
    mockedProbe.mockRejectedValueOnce(axiosError(404))
    const exportCtl = useCharacterBackupExport()
    await exportCtl.submit('char-1', 'a-very-long-password')

    await exportCtl.download('char-1')

    expect(mockedDownload).not.toHaveBeenCalled()
    expect(exportCtl.errorKey.value).toBe('expired')
    expect(exportCtl.job.value).toBeNull()
  })

  it('does nothing when the job is not actually ready yet', async () => {
    const exportCtl = useCharacterBackupExport()

    await exportCtl.download('char-1')

    expect(mockedProbe).not.toHaveBeenCalled()
    expect(mockedDownload).not.toHaveBeenCalled()
  })
})

describe('reset', () => {
  it('drops the job and clears any refusal', async () => {
    mockedStart.mockRejectedValueOnce(axiosError(429))
    const exportCtl = useCharacterBackupExport()
    await exportCtl.submit('char-1', 'a-very-long-password')
    expect(exportCtl.errorKey.value).toBe('rateLimited')

    exportCtl.reset()

    expect(exportCtl.errorKey.value).toBeNull()
    expect(exportCtl.job.value).toBeNull()
    expect(exportCtl.passwordModalOpen.value).toBe(false)
  })

  it('also clears the failed job detail (FE-2)', async () => {
    mockedStart.mockResolvedValueOnce(job())
    mockedPoll.mockResolvedValueOnce(
      job({ status: 'failed', error: 'disk full mid-export' }),
    )
    const exportCtl = useCharacterBackupExport()
    await exportCtl.submit('char-1', 'a-very-long-password')
    expect(exportCtl.errorDetail.value).toBe('disk full mid-export')

    exportCtl.reset()

    expect(exportCtl.errorDetail.value).toBeNull()
  })
})
