/**
 * Crash-recovery persistence for the arc-template intake wizard.
 *
 * The wizard has no server-side draft; a reload / tab-close / crash — or a
 * mis-click that dismisses the modal — used to vaporise a whole authored
 * draft with no undo. These pure helpers autosave the in-progress draft to
 * a storage backend (localStorage in the app) and read it back on reopen.
 *
 * Kept as pure functions with an injected `storage` (mirroring
 * `arcDiscovery.ts`) so the recovery logic is unit-testable without a
 * component mount — the wizard itself has no mount-test harness.
 */

import type { TemplateDraftPayload } from '@/types/arcTemplateIntake'

export const WIZARD_DRAFT_STORAGE_KEY = 'yuralume.arcTemplateWizard.draft'

type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

export interface PersistedWizardDraft {
  draft: TemplateDraftPayload
  pitch: string
  /** 1..6 wizard step; kept loose so the util doesn't import the enum. */
  step: number
}

/** Whether a draft holds work worth guarding / restoring. */
export function draftHasWizardContent(
  draft: TemplateDraftPayload,
  pitch: string,
): boolean {
  return Boolean(
    pitch.trim()
    || draft.title.trim()
    || draft.premise.trim()
    || draft.beats.length > 0,
  )
}

/** Read a previously-autosaved draft; null if absent / blank / corrupt. */
export function loadWizardDraft(
  storage: DraftStorage | null | undefined,
): PersistedWizardDraft | null {
  if (!storage) return null
  try {
    const raw = storage.getItem(WIZARD_DRAFT_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedWizardDraft
    if (!parsed?.draft || !draftHasWizardContent(parsed.draft, parsed.pitch ?? '')) {
      return null
    }
    return parsed
  } catch {
    return null
  }
}

/**
 * Autosave the current draft. Writing a blank draft clears the slot instead
 * so a fresh-but-untouched wizard never leaves a ghost to restore.
 */
export function saveWizardDraft(
  storage: DraftStorage | null | undefined,
  state: PersistedWizardDraft,
): void {
  if (!storage) return
  try {
    if (!draftHasWizardContent(state.draft, state.pitch)) {
      storage.removeItem(WIZARD_DRAFT_STORAGE_KEY)
      return
    }
    storage.setItem(WIZARD_DRAFT_STORAGE_KEY, JSON.stringify(state))
  } catch {
    // Best-effort — storage disabled / full just means no recovery.
  }
}

/** Drop the autosaved draft (after a clean save or a confirmed discard). */
export function clearWizardDraft(
  storage: DraftStorage | null | undefined,
): void {
  if (!storage) return
  try {
    storage.removeItem(WIZARD_DRAFT_STORAGE_KEY)
  } catch {
    // ignore — nothing we can do if storage is unavailable.
  }
}
