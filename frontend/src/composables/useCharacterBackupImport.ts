import { computed, ref } from 'vue'

import {
  getCharacterBackupImportJob,
  previewCharacterBackupImport,
  startCharacterBackupImport,
  uploadCharacterBackupFile,
  type CharacterBackupImportPreview,
  type CharacterBackupJob,
} from '@/utils/api/characterBackups'
import {
  characterBackupConfirmErrorKey,
  characterBackupFallbackMessage,
  characterBackupPreviewErrorKey,
  characterBackupUploadErrorKey,
  type CharacterBackupConfirmErrorKey,
  type CharacterBackupPreviewErrorKey,
  type CharacterBackupUploadErrorKey,
} from '@/utils/characterBackupErrors'
import { pollBackupJob } from '@/utils/characterBackupPolling'

export type CharacterBackupImportPhase =
  | 'idle'
  | 'uploading'
  | 'passwordPrompt'
  | 'previewing'
  | 'previewReady'
  | 'confirming'
  | 'restoring'
  | 'succeeded'
  | 'failed'

export type CharacterBackupImportErrorKey =
  | CharacterBackupUploadErrorKey
  | CharacterBackupPreviewErrorKey
  | CharacterBackupConfirmErrorKey

/**
 * The import half of the backup/restore panel (CB4): stage a `.lumebackup`
 * upload, unlock and preview it, confirm into a restore job, and follow
 * that job to a new character.
 *
 * Restoring a backup *creates a character* — it never touches the character
 * whose settings page it was opened from — so this composable never takes a
 * `characterId`; the id it eventually produces (`job.character_id`) is a
 * brand-new one, fetched and handed to the caller by the owning panel.
 */
export function useCharacterBackupImport() {
  const phase = ref<CharacterBackupImportPhase>('idle')
  const passwordModalOpen = ref(false)
  const fileName = ref<string | null>(null)
  const stagedObjectKey = ref<string | null>(null)
  /** Held only in memory for the lifetime of one import — never logged,
   * never sent anywhere but the two calls that need it, cleared by
   * `reset()`. */
  const unlockedPassword = ref('')
  const preview = ref<CharacterBackupImportPreview | null>(null)
  const job = ref<CharacterBackupJob | null>(null)
  const errorKey = ref<CharacterBackupImportErrorKey | 'generic' | null>(null)
  /** Raw server text — only ever populated for `createGate`, the one
   * refusal with no fixed catalog copy (the dynamic slot-cap / daily-limit
   * message), and for a job that failed mid-pipeline (`job.error`, a
   * ``str(exc)`` last resort the same way `characterCreate` shows one). */
  const errorDetail = ref<string | null>(null)

  /** Bumped on every `selectFile()` / `reset()` so a stale upload, preview,
   * or poll can never write onto a flow the player has since restarted. */
  let generation = 0

  const busy = computed(() => (
    phase.value === 'uploading'
    || phase.value === 'previewing'
    || phase.value === 'confirming'
    || phase.value === 'restoring'
  ))

  async function selectFile(file: File): Promise<void> {
    reset()
    const myGeneration = generation
    fileName.value = file.name
    phase.value = 'uploading'
    try {
      const staged = await uploadCharacterBackupFile(file)
      if (myGeneration !== generation) return
      stagedObjectKey.value = staged.staged_object_key
      phase.value = 'passwordPrompt'
      passwordModalOpen.value = true
    } catch (error) {
      if (myGeneration !== generation) return
      errorKey.value = characterBackupUploadErrorKey(error)
      phase.value = 'failed'
    }
  }

  async function submitPassword(password: string): Promise<void> {
    const stagedKey = stagedObjectKey.value
    if (!stagedKey || phase.value === 'previewing') return
    const myGeneration = generation
    phase.value = 'previewing'
    errorKey.value = null
    errorDetail.value = null
    try {
      const result = await previewCharacterBackupImport(stagedKey, password)
      if (myGeneration !== generation) return
      preview.value = result
      unlockedPassword.value = password
      passwordModalOpen.value = false
      phase.value = 'previewReady'
    } catch (error) {
      if (myGeneration !== generation) return
      const key = characterBackupPreviewErrorKey(error)
      errorKey.value = key
      if (key === 'wrongPassword') {
        // The upload is still staged — let the player retype the password
        // without going back to pick the file again.
        phase.value = 'passwordPrompt'
      } else {
        passwordModalOpen.value = false
        phase.value = 'failed'
      }
    }
  }

  async function confirm(): Promise<void> {
    const stagedKey = stagedObjectKey.value
    if (!stagedKey || !unlockedPassword.value || phase.value !== 'previewReady') return
    const myGeneration = generation
    phase.value = 'confirming'
    errorKey.value = null
    errorDetail.value = null
    try {
      const started = await startCharacterBackupImport(stagedKey, unlockedPassword.value)
      if (myGeneration !== generation) return
      job.value = started
      phase.value = 'restoring'
      await runPoll(started.job_id, myGeneration)
    } catch (error) {
      if (myGeneration !== generation) return
      const key = characterBackupConfirmErrorKey(error)
      errorKey.value = key
      if (key === 'createGate') {
        errorDetail.value = characterBackupFallbackMessage(error)
      }
      if (key === 'wrongPassword') {
        // Consumed once already at preview — a refusal here means it no
        // longer unlocks the file (e.g. the staged upload was replaced).
        // Send the player back to re-enter it rather than guessing.
        phase.value = 'passwordPrompt'
        passwordModalOpen.value = true
      } else {
        phase.value = 'failed'
      }
    }
  }

  async function runPoll(jobId: string, myGeneration: number): Promise<void> {
    try {
      await pollBackupJob({
        fetch: () => getCharacterBackupImportJob(jobId),
        isCancelled: () => myGeneration !== generation,
        onUpdate: (updated) => {
          if (myGeneration !== generation) return
          job.value = updated
        },
      })
      if (myGeneration !== generation) return
      if (job.value?.status === 'succeeded') {
        phase.value = 'succeeded'
        return
      }
      if (job.value?.status === 'failed') {
        errorKey.value = 'generic'
        errorDetail.value = job.value.error
        phase.value = 'failed'
      }
    } catch {
      if (myGeneration !== generation) return
      // A read failure is "we could not check", not "the restore failed" —
      // but unlike the export side there is no button left to press here,
      // so it has to land somewhere the player can act on (retry from
      // scratch) rather than spin forever.
      errorKey.value = 'generic'
      phase.value = 'failed'
    }
  }

  function closePasswordModal() {
    if (phase.value === 'previewing') return
    passwordModalOpen.value = false
    if (phase.value === 'passwordPrompt') reset()
  }

  function dismissError() {
    errorKey.value = null
    errorDetail.value = null
  }

  function reset() {
    generation += 1
    phase.value = 'idle'
    passwordModalOpen.value = false
    fileName.value = null
    stagedObjectKey.value = null
    unlockedPassword.value = ''
    preview.value = null
    job.value = null
    errorKey.value = null
    errorDetail.value = null
  }

  return {
    phase,
    passwordModalOpen,
    fileName,
    preview,
    job,
    errorKey,
    errorDetail,
    busy,
    selectFile,
    submitPassword,
    confirm,
    closePasswordModal,
    dismissError,
    reset,
  }
}

export type CharacterBackupImportController = ReturnType<typeof useCharacterBackupImport>
