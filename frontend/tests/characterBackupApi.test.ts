import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * The backup/restore transport (CB2/CB3 contract, CB4 client). Pinned here:
 * the multipart field name the upload route expects (`backup`, not
 * `file`/`card`), that the download URL carries the `?access_token=`
 * bearer-equivalent fallback the native `<a>` download needs (mirrors
 * `downloadCharacterCard`), and that the TTL-expiry probe hits the exact
 * same path with no query token at all (it goes through axios, which
 * already attaches `Authorization` via the app's request interceptor).
 */

const authHolder = vi.hoisted(() => ({ token: null as string | null }))

vi.mock('axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), head: vi.fn() },
}))
vi.mock('@/composables/useAuth', () => ({
  getStoredToken: () => authHolder.token,
}))

const axios = (await import('axios')).default
const {
  downloadCharacterBackup,
  getCharacterBackupExportJob,
  getCharacterBackupImportJob,
  previewCharacterBackupImport,
  probeCharacterBackupDownload,
  startCharacterBackupExport,
  startCharacterBackupImport,
  uploadCharacterBackupFile,
} = await import('@/utils/api/characterBackups')

const mockedAxios = vi.mocked(axios, true)

beforeEach(() => {
  vi.clearAllMocks()
  authHolder.token = null
})

describe('export', () => {
  it('posts the password to start a backup', async () => {
    mockedAxios.post.mockResolvedValueOnce({ data: { job_id: 'job-1' } })

    await startCharacterBackupExport('char-1', 'hunter2long')

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/characters/char-1/backup',
      { password: 'hunter2long' },
    )
  })

  it('polls the export job by id', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: { job_id: 'job-1', status: 'running' } })

    await getCharacterBackupExportJob('char-1', 'job-1')

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/char-1/backup/jobs/job-1',
    )
  })
})

// This repo's test environment is plain Node — no jsdom/happy-dom (see
// `fusionStoryExitHub.test.ts`'s comment). `downloadCharacterBackup` touches
// `document`, so it gets the smallest possible stand-in rather than a real
// DOM, just enough to capture what the native `<a>` download would have
// pointed at.
interface StubAnchor {
  tagName: string
  href: string
  rel: string
  style: Record<string, string>
  clicked: boolean
  removed: boolean
  click(): void
  remove(): void
}

function stubDocument(): { anchors: StubAnchor[] } {
  const anchors: StubAnchor[] = []
  ;(globalThis as { document?: unknown }).document = {
    createElement: (tag: string): StubAnchor => {
      const el: StubAnchor = {
        tagName: tag,
        href: '',
        rel: '',
        style: {},
        clicked: false,
        removed: false,
        click() { this.clicked = true },
        remove() { this.removed = true },
      }
      if (tag === 'a') anchors.push(el)
      return el
    },
    body: { appendChild: (node: unknown) => node },
  }
  return { anchors }
}

describe('download', () => {
  afterEach(() => {
    delete (globalThis as { document?: unknown }).document
  })

  it('builds the download URL with the bearer-equivalent access_token', () => {
    authHolder.token = 'secret-token'
    const { anchors } = stubDocument()

    downloadCharacterBackup('char-1', 'job-1')

    expect(anchors).toHaveLength(1)
    expect(anchors[0].href).toContain('/api/v1/characters/char-1/backup/jobs/job-1/download')
    expect(anchors[0].href).toContain('access_token=secret-token')
    expect(anchors[0].clicked).toBe(true)
    expect(anchors[0].removed).toBe(true)
  })

  it('omits the query token entirely when signed out', () => {
    authHolder.token = null
    const { anchors } = stubDocument()

    downloadCharacterBackup('char-1', 'job-1')

    expect(anchors[0].href).not.toContain('access_token')
  })

  it('probes with a plain HEAD — no query token, axios carries auth', async () => {
    mockedAxios.head.mockResolvedValueOnce({ status: 200 })

    await probeCharacterBackupDownload('char-1', 'job-1')

    expect(mockedAxios.head).toHaveBeenCalledWith(
      '/api/v1/characters/char-1/backup/jobs/job-1/download',
    )
  })
})

describe('import', () => {
  it('uploads under the field name the route expects', async () => {
    mockedAxios.post.mockResolvedValueOnce({
      data: { staged_object_key: 'staged-1', size_bytes: 1024 },
    })
    const file = new File(['x'], 'aria.lumebackup')

    await uploadCharacterBackupFile(file)

    const [url, body, config] = mockedAxios.post.mock.calls[0]
    expect(url).toBe('/api/v1/characters/backup/upload')
    expect(body).toBeInstanceOf(FormData)
    expect((body as FormData).get('backup')).toBe(file)
    expect((config as { headers: Record<string, string> }).headers['Content-Type'])
      .toBe('multipart/form-data')
  })

  it('previews with the staged key and password', async () => {
    mockedAxios.post.mockResolvedValueOnce({ data: { character_name: 'Aria' } })

    await previewCharacterBackupImport('staged-1', 'hunter2long')

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/characters/backup/preview',
      { staged_object_key: 'staged-1', password: 'hunter2long' },
    )
  })

  it('confirms into a restore job with the same body shape as preview', async () => {
    mockedAxios.post.mockResolvedValueOnce({ data: { job_id: 'restore-1' } })

    await startCharacterBackupImport('staged-1', 'hunter2long')

    expect(mockedAxios.post).toHaveBeenCalledWith(
      '/api/v1/characters/backup/import',
      { staged_object_key: 'staged-1', password: 'hunter2long' },
    )
  })

  it('polls the restore job with no character in the path', async () => {
    mockedAxios.get.mockResolvedValueOnce({ data: { job_id: 'restore-1', status: 'running' } })

    await getCharacterBackupImportJob('restore-1')

    expect(mockedAxios.get).toHaveBeenCalledWith(
      '/api/v1/characters/backup/jobs/restore-1',
    )
  })
})
