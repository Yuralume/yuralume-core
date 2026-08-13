import axios from 'axios'
import { getStoredToken } from '@/composables/useAuth'

const CHARACTERS_BASE = '/api/v1/characters'
const IMPORT_BASE = '/api/v1/characters/backup'

export type CharacterBackupJobKind = 'export' | 'restore'
export type CharacterBackupJobStatus = 'running' | 'succeeded' | 'failed'

/** ``progress.stage`` values the export pipeline checkpoints through, in
 * order (``character_backup_export_service.py``). */
export const CHARACTER_BACKUP_EXPORT_STAGES = [
  'queued', 'dump', 'media', 'arc_templates', 'encrypt', 'upload',
] as const

/** ``progress.stage`` values the restore pipeline checkpoints through, in
 * order (``character_backup_import_service.py``). ``failed`` is a terminal
 * marker, not a step, so it is not counted for progress display. */
export const CHARACTER_BACKUP_RESTORE_STAGES = [
  'queued', 'download', 'decrypt', 'scan', 'gates', 'arc_templates',
  'rows', 'media', 'embeddings', 'done',
] as const

export interface CharacterBackupRestoreEmbeddingReport {
  status: 'done' | 'pending' | 'failed'
  content_vectors: number
  tag_vectors: number
}

/** The terminal restore ``report`` — CB2/CB3 contract, shared by the ticket
 * and ``character_backup_restore_plan.py``'s ``to_progress()``. */
export interface CharacterBackupRestoreReport {
  nsfw_messages_replaced: number
  nsfw_messages_dropped: number
  nsfw_memories_skipped: number
  nsfw_operator_profile_fields_skipped: number
  nsfw_pending_follow_ups_skipped: number
  media_transferred: number
  media_missing: string[]
  rows_landed: Record<string, number>
  rows_skipped: Record<string, number>
  landed_arc_template_ids: string[]
  landed_arc_series_ids: string[]
  seed_refs_cleared: number
  embedding: CharacterBackupRestoreEmbeddingReport
}

/** Poll shape shared by export and restore jobs. */
export interface CharacterBackupJob {
  job_id: string
  kind: CharacterBackupJobKind
  character_id: string
  status: CharacterBackupJobStatus
  stage: string
  error: string | null
  download_ready: boolean
  report: CharacterBackupRestoreReport | null
  created_at: string
  finished_at: string | null
}

export interface StagedBackupUpload {
  staged_object_key: string
  size_bytes: number
}

export interface CharacterBackupNsfwCollapseStats {
  messages_replaced: number
  messages_dropped: number
  memories_skipped: number
  operator_profile_fields_skipped: number
  pending_follow_ups_skipped: number
}

export interface CharacterBackupExportReport {
  skipped_media_keys: string[]
  notes: string[]
}

/** What the import confirm dialog shows — manifest-derived only, before any
 * data is actually read (``CharacterBackupImportPreview`` on the backend). */
export interface CharacterBackupImportPreview {
  staged_object_key: string
  character_name: string
  original_character_id: string
  schema_version: number
  app_version: string
  exported_at: string
  table_counts: Record<string, number>
  total_rows: number
  media_count: number
  media_total_bytes: number
  nsfw_collapse: CharacterBackupNsfwCollapseStats
  will_collapse_nsfw: boolean
  export_report: CharacterBackupExportReport
}

/**
 * Set a one-time password and start a full backup export for one character.
 *
 * 202 with the running job; 409 while an export for this character is
 * already in flight, 429 past the hosted throttle, 503 while the export
 * pipeline is not configured on this deployment.
 */
export async function startCharacterBackupExport(
  characterId: string,
  password: string,
): Promise<CharacterBackupJob> {
  const { data } = await axios.post<CharacterBackupJob>(
    `${CHARACTERS_BASE}/${encodeURIComponent(characterId)}/backup`,
    { password },
  )
  return data
}

export async function getCharacterBackupExportJob(
  characterId: string,
  jobId: string,
): Promise<CharacterBackupJob> {
  const { data } = await axios.get<CharacterBackupJob>(
    `${CHARACTERS_BASE}/${encodeURIComponent(characterId)}/backup/jobs/${encodeURIComponent(jobId)}`,
  )
  return data
}

/**
 * 觸發加密備份檔（``.lumebackup``）原生下載。同 ``downloadCharacterCard``
 * 的理由：native `<a>` 下載交給後端 Content-Disposition 決定檔名（含 CJK
 * 檔名的 RFC 5987 形式），刻意不用 axios blob（見該函式註解）。
 */
export function downloadCharacterBackup(characterId: string, jobId: string): void {
  const url = buildCharacterBackupDownloadUrl(characterId, jobId)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.rel = 'noopener'
  anchor.style.display = 'none'
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
}

function characterBackupDownloadPath(characterId: string, jobId: string): string {
  return `${CHARACTERS_BASE}/${encodeURIComponent(characterId)}/backup/jobs/${encodeURIComponent(jobId)}/download`
}

function buildCharacterBackupDownloadUrl(characterId: string, jobId: string): string {
  const params = new URLSearchParams()
  const token = getStoredToken()
  if (token) {
    // Native downloads cannot send Authorization headers — same
    // ?access_token= bearer-equivalent fallback as the card download.
    params.set('access_token', token)
  }
  const query = params.toString()
  return `${characterBackupDownloadPath(characterId, jobId)}${query ? `?${query}` : ''}`
}

/**
 * Probe the download endpoint before committing to the native `<a>`
 * download below. A `404` here means the ephemeral artifact was TTL-swept
 * since the panel's last poll — the one failure a plain navigation click
 * cannot observe (a non-2xx JSON response carries no
 * ``Content-Disposition``, so clicking straight through would navigate the
 * whole SPA tab away to raw JSON instead of showing an in-app message).
 * ``HEAD`` carries no body, so this costs nothing beyond the round trip.
 */
export async function probeCharacterBackupDownload(
  characterId: string,
  jobId: string,
): Promise<void> {
  await axios.head(characterBackupDownloadPath(characterId, jobId))
}

/**
 * Stage an uploaded ``.lumebackup`` for preview / import. 400 when it is
 * not a readable backup archive, 413 past the size cap.
 */
export async function uploadCharacterBackupFile(file: File): Promise<StagedBackupUpload> {
  const form = new FormData()
  form.append('backup', file)
  const { data } = await axios.post<StagedBackupUpload>(
    `${IMPORT_BASE}/upload`,
    form,
    { headers: { 'Content-Type': 'multipart/form-data' } },
  )
  return data
}

/** Verify the password and preview what the import would restore. */
export async function previewCharacterBackupImport(
  stagedObjectKey: string,
  password: string,
): Promise<CharacterBackupImportPreview> {
  const { data } = await axios.post<CharacterBackupImportPreview>(
    `${IMPORT_BASE}/preview`,
    { staged_object_key: stagedObjectKey, password },
  )
  return data
}

/**
 * Confirm a previewed backup into a background restore job. The create
 * gates (slot cap / daily limit) run here — a restore creates a character,
 * same as ``POST /characters`` — and answer the same 400 grammar.
 */
export async function startCharacterBackupImport(
  stagedObjectKey: string,
  password: string,
): Promise<CharacterBackupJob> {
  const { data } = await axios.post<CharacterBackupJob>(
    `${IMPORT_BASE}/import`,
    { staged_object_key: stagedObjectKey, password },
  )
  return data
}

export async function getCharacterBackupImportJob(jobId: string): Promise<CharacterBackupJob> {
  const { data } = await axios.get<CharacterBackupJob>(
    `${IMPORT_BASE}/jobs/${encodeURIComponent(jobId)}`,
  )
  return data
}
