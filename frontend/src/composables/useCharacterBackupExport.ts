import { ref } from 'vue'

import {
  downloadCharacterBackup,
  getCharacterBackupExportJob,
  probeCharacterBackupDownload,
  startCharacterBackupExport,
  type CharacterBackupJob,
} from '@/utils/api/characterBackups'
import {
  characterBackupDownloadErrorKey,
  characterBackupExportStartErrorKey,
  type CharacterBackupDownloadErrorKey,
  type CharacterBackupExportStartErrorKey,
} from '@/utils/characterBackupErrors'
import { pollBackupJob } from '@/utils/characterBackupPolling'

export type CharacterBackupExportErrorKey =
  | CharacterBackupExportStartErrorKey
  | CharacterBackupDownloadErrorKey

/**
 * The export half of the backup/restore panel (CB4): set a one-time
 * password, run the export job to completion, offer the download.
 *
 * A factory, not a singleton — one panel instance owns one character's
 * export state, the same reasoning `useStoryScene` documents for its own
 * per-thread state (a module-level ref would leak one character's export
 * job into the next character's settings page).
 */
export function useCharacterBackupExport() {
  const passwordModalOpen = ref(false)
  const submitting = ref(false)
  const polling = ref(false)
  const checkingDownload = ref(false)
  const job = ref<CharacterBackupJob | null>(null)
  const errorKey = ref<CharacterBackupExportErrorKey | null>(null)
  /** Raw server text for a job that failed mid-pipeline (``job.error``) —
   * the import side's ``errorDetail`` precedent. Only ever populated
   * alongside ``errorKey === 'generic'`` from a terminal ``failed`` job. */
  const errorDetail = ref<string | null>(null)

  /** Bumped on every new job so a poll loop for a job the player has
   * already replaced (a fresh export, a `reset()`) can never write its
   * answer onto the state the panel is now showing. */
  let generation = 0

  function openPasswordModal() {
    errorKey.value = null
    passwordModalOpen.value = true
  }

  function closePasswordModal() {
    if (submitting.value) return
    passwordModalOpen.value = false
  }

  async function submit(characterId: string, password: string): Promise<void> {
    if (submitting.value) return
    submitting.value = true
    errorKey.value = null
    errorDetail.value = null
    const myGeneration = ++generation
    job.value = null
    try {
      const started = await startCharacterBackupExport(characterId, password)
      if (myGeneration !== generation) return
      job.value = started
      passwordModalOpen.value = false
      await runPoll(characterId, started.job_id, myGeneration)
    } catch (error) {
      if (myGeneration !== generation) return
      // Nothing about a start failure (already running, rate limited, not
      // configured) is fixed by retyping the password, so the modal is
      // closed and the refusal surfaces as a toast (panel-level, CB4 UX
      // note) rather than staying pinned to a form the player would just
      // resubmit unchanged.
      passwordModalOpen.value = false
      errorKey.value = characterBackupExportStartErrorKey(error)
    } finally {
      if (myGeneration === generation) submitting.value = false
    }
  }

  async function runPoll(
    characterId: string,
    jobId: string,
    myGeneration: number,
  ): Promise<void> {
    polling.value = true
    try {
      await pollBackupJob({
        fetch: () => getCharacterBackupExportJob(characterId, jobId),
        isCancelled: () => myGeneration !== generation,
        onUpdate: (updated) => {
          if (myGeneration !== generation) return
          job.value = updated
        },
      })
      if (myGeneration !== generation) return
      // `pollBackupJob` resolves normally on ANY terminal status, not just
      // success — a `failed` job is not a fetch error, so it never reached
      // the `catch` below and previously left the progress bar simply
      // vanishing with no refusal surfaced anywhere (FE-2). Mirror the
      // import side's `runPoll`: promote the job's own `error` into the
      // same errorKey/toast path a start refusal already uses.
      if (job.value?.status === 'failed') {
        errorKey.value = 'generic'
        errorDetail.value = job.value.error
      }
    } catch {
      if (myGeneration !== generation) return
      // A read failure here is not "the export failed" — it is "we could
      // not check". Keep the last known job on screen; the stage line
      // simply stops advancing rather than claiming a verdict we don't
      // have.
      errorKey.value = 'generic'
    } finally {
      if (myGeneration === generation) polling.value = false
    }
  }

  /**
   * Download the finished artifact. Probes the endpoint first so a TTL
   * sweep since the last poll answers as an in-app "expired, export again"
   * message rather than navigating the whole tab to raw JSON (see
   * `probeCharacterBackupDownload`).
   */
  async function download(characterId: string): Promise<void> {
    const current = job.value
    if (!current || !current.download_ready || checkingDownload.value) return
    errorKey.value = null
    checkingDownload.value = true
    try {
      await probeCharacterBackupDownload(characterId, current.job_id)
      downloadCharacterBackup(characterId, current.job_id)
    } catch (error) {
      const key = characterBackupDownloadErrorKey(error)
      errorKey.value = key
      // The artifact is gone — showing a download button that can only
      // ever 404 again is a dead end. Drop the job so the panel falls back
      // to the ordinary "start a backup" button instead.
      if (key === 'expired') job.value = null
    } finally {
      checkingDownload.value = false
    }
  }

  function dismissError() {
    errorKey.value = null
    errorDetail.value = null
  }

  /** Drop everything and forget the last job — used when the panel's
   * character changes, or the player wants to start a brand-new export
   * over a finished or failed one. */
  function reset() {
    generation += 1
    passwordModalOpen.value = false
    submitting.value = false
    polling.value = false
    checkingDownload.value = false
    job.value = null
    errorKey.value = null
    errorDetail.value = null
  }

  return {
    passwordModalOpen,
    submitting,
    polling,
    checkingDownload,
    job,
    errorKey,
    errorDetail,
    openPasswordModal,
    closePasswordModal,
    submit,
    download,
    dismissError,
    reset,
  }
}

export type CharacterBackupExportController = ReturnType<typeof useCharacterBackupExport>
