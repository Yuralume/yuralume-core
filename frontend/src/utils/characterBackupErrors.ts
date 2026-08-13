import axios from 'axios'

/**
 * Refusal classification for the character-backup surface (CB4).
 *
 * The backup routes answer plain ``{"detail": "<string>"}`` — there is no
 * structured ``code`` the way ``story_scene`` or ``price_changed`` have one
 * (``api/routes/character_backups.py`` module docstring: "the error grammar
 * is kept deliberately small"). So classification here leans on the status
 * code, which is unambiguous *within one call site* — every route only ever
 * answers a given status for one reason — except the shared ``400`` on the
 * import routes, which is genuinely three different refusals wearing the
 * same status and needs the exact ``detail`` text to tell apart.
 *
 * Copy lives in the trilingual catalog under ``characterBackup.errors.*``;
 * nothing here is shown directly, so a raw server string never reaches the
 * player except as the one deliberate exception (``createGate``, below).
 */

/** The two ``400`` detail strings the import routes emit verbatim (never
 * ``str(exc)``) — see ``_map_import_file_error`` in
 * ``character_backups.py``. Any other 400 on the import-confirm route is
 * the character-create gate (slot cap / daily limit), which *is*
 * ``str(exc)`` and therefore has no fixed text to match. */
const WRONG_PASSWORD_DETAIL = 'Wrong backup password'
const CORRUPT_FILE_DETAIL = 'Backup file is unreadable, corrupted or tampered with'

export function characterBackupStatusCode(error: unknown): number | null {
  if (!axios.isAxiosError(error)) return null
  const status = error.response?.status
  return typeof status === 'number' ? status : null
}

export function characterBackupErrorDetail(error: unknown): string | null {
  if (!axios.isAxiosError(error)) return null
  const detail = (error.response?.data as { detail?: unknown } | undefined)?.detail
  return typeof detail === 'string' && detail.trim() ? detail : null
}

export function isBackupWrongPassword(error: unknown): boolean {
  return (
    characterBackupStatusCode(error) === 400
    && characterBackupErrorDetail(error) === WRONG_PASSWORD_DETAIL
  )
}

function isBackupCorruptFile(error: unknown): boolean {
  return (
    characterBackupStatusCode(error) === 400
    && characterBackupErrorDetail(error) === CORRUPT_FILE_DETAIL
  )
}

/** Fallback text for a refusal with no fixed copy: the raw server detail,
 * else ``Error#message``, else null. Only ever shown for `createGate`
 * (the dynamic slot-cap / daily-limit message a restore walks into). */
export function characterBackupFallbackMessage(error: unknown): string | null {
  return characterBackupErrorDetail(error) ?? (error instanceof Error ? error.message : null)
}

export type CharacterBackupExportStartErrorKey =
  | 'conflict' | 'rateLimited' | 'unavailable' | 'notFound' | 'generic'

/** ``POST /characters/{id}/backup``. */
export function characterBackupExportStartErrorKey(
  error: unknown,
): CharacterBackupExportStartErrorKey {
  switch (characterBackupStatusCode(error)) {
    case 409: return 'conflict'
    case 429: return 'rateLimited'
    case 503: return 'unavailable'
    case 404: return 'notFound'
    default: return 'generic'
  }
}

export type CharacterBackupDownloadErrorKey = 'expired' | 'notReady' | 'generic'

/** ``GET .../backup/jobs/{id}/download``. */
export function characterBackupDownloadErrorKey(error: unknown): CharacterBackupDownloadErrorKey {
  switch (characterBackupStatusCode(error)) {
    case 404: return 'expired'
    case 409: return 'notReady'
    default: return 'generic'
  }
}

export type CharacterBackupUploadErrorKey = 'invalidFile' | 'tooLarge' | 'unavailable' | 'generic'

/** ``POST /characters/backup/upload``. */
export function characterBackupUploadErrorKey(error: unknown): CharacterBackupUploadErrorKey {
  switch (characterBackupStatusCode(error)) {
    case 400: return 'invalidFile'
    case 413: return 'tooLarge'
    case 503: return 'unavailable'
    default: return 'generic'
  }
}

export type CharacterBackupPreviewErrorKey =
  | 'wrongPassword' | 'corruptFile' | 'expired' | 'schemaTooNew' | 'tooLarge' | 'generic'

/** ``POST /characters/backup/preview`` — the create gates never run here
 * (they run at confirm), so a 400 is only ever the password or the file. */
export function characterBackupPreviewErrorKey(error: unknown): CharacterBackupPreviewErrorKey {
  const status = characterBackupStatusCode(error)
  if (status === 400) return isBackupWrongPassword(error) ? 'wrongPassword' : 'corruptFile'
  switch (status) {
    case 404: return 'expired'
    case 422: return 'schemaTooNew'
    case 413: return 'tooLarge'
    default: return 'generic'
  }
}

export type CharacterBackupConfirmErrorKey =
  | 'wrongPassword' | 'corruptFile' | 'createGate' | 'expired' | 'schemaTooNew'
  | 'tooLarge' | 'generic'

/** ``POST /characters/backup/import`` — a 400 here can additionally be the
 * character-create gate, since confirming a restore creates a character. */
export function characterBackupConfirmErrorKey(error: unknown): CharacterBackupConfirmErrorKey {
  const status = characterBackupStatusCode(error)
  if (status === 400) {
    if (isBackupWrongPassword(error)) return 'wrongPassword'
    if (isBackupCorruptFile(error)) return 'corruptFile'
    return 'createGate'
  }
  switch (status) {
    case 404: return 'expired'
    case 422: return 'schemaTooNew'
    case 413: return 'tooLarge'
    default: return 'generic'
  }
}
