/**
 * Shared "翻成我的語言" translation state for arc-template surfaces.
 *
 * Extracted from `ArcTemplatePicker.vue` (plan D1 — kill the fork) so the
 * picker and the Creator Studio list consume one mechanism instead of two
 * diverging copies. Both the picker and studio pass their `templates` ref
 * and render `displayTemplate(tpl)`.
 *
 * Semantics (plan D3/D4 + A2 hardening + P4):
 * - **Read-only / never persisted.** Translations live in an in-memory
 *   cache on this composable instance; they are never written to a series
 *   payload or the DB. Callers always bind the original `template.id`.
 * - **Fail-soft.** A translation failure keeps the authored prose (the
 *   card never blanks) but is NOT cached as a success — it is marked
 *   failed so the operator can retry (A2 fix for the old "silent failure
 *   cached forever" bug).
 * - **Target = operator primary language** (injected via `targetLanguage`;
 *   see `useOperatorLanguage`), not the transient UI locale — so a preview
 *   matches what materialise will actually produce (P4).
 * - **Cache key includes the target language** so switching language mid
 *   session never serves a stale other-language translation (P4 bug fix).
 * - **Content-signature invalidation.** A cached entry is only used when
 *   the source prose still matches — so re-editing a template (PATCH
 *   overwrite → list reload) never shows a stale translation.
 * - **Incremental + capped.** Each card's translation is applied the
 *   moment it resolves (fast cards switch first), with a small concurrency
 *   cap so a large catalogue doesn't fan out N simultaneous LLM calls (A2).
 * - **Instance-scoped cache.** A cross-mount / server-side shared cache is
 *   an independent backend batch (out of scope here); reopening a surface
 *   re-fetches, which the deferred backend cache will absorb.
 *
 * Node-testable: `targetLanguage`, `previewFn` and `storage` are all
 * injectable, so the logic can be unit-tested without a component mount.
 */

import { computed, getCurrentScope, onScopeDispose, ref, watch, type Ref } from 'vue'
import type { ArcTemplate } from '@/types/arcTemplate'
import { previewArcTemplateTranslation } from '@/utils/api/arcTemplates'

type PreviewFn = (id: string, targetLanguage: string) => Promise<ArcTemplate>
type FlagStorage = Pick<Storage, 'getItem' | 'setItem'>

export interface UseArcTemplateTranslationOptions {
  /** Authoritative translation target (operator primary language). */
  targetLanguage?: () => string
  /** Injectable translate call (defaults to the real REST client). */
  previewFn?: PreviewFn
  /** localStorage key for remembering the toggle; null = don't persist. */
  persistKey?: string | null
  /** Storage backend (defaults to window.localStorage when available). */
  storage?: FlagStorage | null
  /** Max simultaneous in-flight translations. */
  concurrency?: number
}

interface CacheEntry {
  /** Normalised target language this translation was produced for. */
  target: string
  /** Signature of the source prose the translation was produced from. */
  signature: string
  template: ArcTemplate
}

function normaliseLang(value: string | null | undefined): string {
  return (value ?? '').trim().toLowerCase()
}

/** Stable signature of the translatable prose so edits invalidate cache. */
function sourceSignature(tpl: ArcTemplate): string {
  const beats = tpl.beats
    .map((b) =>
      [
        b.title,
        b.summary,
        b.location ?? '',
        b.dramatic_question ?? '',
        b.scene_characters.join(','),
      ].join(''),
    )
    .join('')
  return [tpl.title, tpl.premise, beats].join('')
}

function defaultStorage(): FlagStorage | null {
  try {
    if (typeof window !== 'undefined' && window.localStorage) {
      return window.localStorage
    }
  } catch {
    // localStorage can throw in privacy modes — degrade to no persistence.
  }
  return null
}

export function useArcTemplateTranslation(
  templates: Ref<ArcTemplate[]>,
  options: UseArcTemplateTranslationOptions = {},
) {
  const targetLanguage = options.targetLanguage ?? (() => '')
  const previewFn: PreviewFn = options.previewFn ?? previewArcTemplateTranslation
  const concurrency = Math.max(1, options.concurrency ?? 4)
  const persistKey = options.persistKey ?? null
  const storage = options.storage === undefined ? defaultStorage() : options.storage

  const cache = ref<Record<string, CacheEntry>>({})
  const failedIds = ref<string[]>([])
  const inFlightCount = ref(0)
  const inFlightKeys = new Set<string>()
  let disposed = false

  function readStoredFlag(): boolean {
    if (!persistKey || !storage) return false
    try {
      return storage.getItem(persistKey) === '1'
    } catch {
      return false
    }
  }

  function writeStoredFlag(value: boolean): void {
    if (!persistKey || !storage) return
    try {
      storage.setItem(persistKey, value ? '1' : '0')
    } catch {
      // Best-effort — a failed write just means the toggle isn't remembered.
    }
  }

  const translateEnabled = ref(readStoredFlag())
  const translating = computed(() => inFlightCount.value > 0)
  const hasFailures = computed(() => failedIds.value.length > 0)

  function cacheKey(id: string, target: string): string {
    return `${id}::${target}`
  }

  function freshEntry(tpl: ArcTemplate): CacheEntry | null {
    const target = normaliseLang(targetLanguage())
    if (!target) return null
    const entry = cache.value[cacheKey(tpl.id, target)]
    if (!entry) return null
    if (entry.target !== target) return null
    if (entry.signature !== sourceSignature(tpl)) return null
    return entry
  }

  /** Whether a template's authored language differs from the target. */
  function needsTranslation(tpl: ArcTemplate): boolean {
    const target = normaliseLang(targetLanguage())
    if (!target) return false
    return normaliseLang(tpl.language) !== target
  }

  /** The view of a template respecting the toggle (cached, fail-soft). */
  function displayTemplate(tpl: ArcTemplate): ArcTemplate {
    if (!translateEnabled.value) return tpl
    if (!needsTranslation(tpl)) return tpl
    return freshEntry(tpl)?.template ?? tpl
  }

  function markFailed(id: string): void {
    if (!failedIds.value.includes(id)) {
      failedIds.value = [...failedIds.value, id]
    }
  }

  function clearFailed(id: string): void {
    if (failedIds.value.includes(id)) {
      failedIds.value = failedIds.value.filter((x) => x !== id)
    }
  }

  /** Build one fetch task per translatable, uncached, not-in-flight card. */
  function scheduleTasks(list: ArcTemplate[]): Array<() => Promise<void>> {
    const rawTarget = targetLanguage()
    const target = normaliseLang(rawTarget)
    if (!target) return []
    const tasks: Array<() => Promise<void>> = []
    for (const tpl of list) {
      if (!needsTranslation(tpl)) continue
      if (freshEntry(tpl)) continue
      const key = cacheKey(tpl.id, target)
      if (inFlightKeys.has(key)) continue
      inFlightKeys.add(key)
      inFlightCount.value += 1
      const signature = sourceSignature(tpl)
      tasks.push(async () => {
        try {
          const translated = await previewFn(tpl.id, rawTarget)
          if (disposed) return
          cache.value = {
            ...cache.value,
            [key]: { target, signature, template: translated },
          }
          clearFailed(tpl.id)
        } catch {
          // Fail-soft: keep authored prose, mark failed so it can retry.
          if (!disposed) markFailed(tpl.id)
        } finally {
          inFlightKeys.delete(key)
          inFlightCount.value -= 1
        }
      })
    }
    return tasks
  }

  /** Run tasks with a bounded concurrency so large lists don't burst. */
  async function pump(tasks: Array<() => Promise<void>>): Promise<void> {
    if (tasks.length === 0) return
    const queue = [...tasks]
    const runNext = async (): Promise<void> => {
      const task = queue.shift()
      if (!task) return
      await task()
      return runNext()
    }
    const lanes = Math.min(concurrency, tasks.length)
    await Promise.all(Array.from({ length: lanes }, () => runNext()))
  }

  async function translate(list: ArcTemplate[]): Promise<void> {
    await pump(scheduleTasks(list))
  }

  function setEnabled(next: boolean): void {
    if (translateEnabled.value === next) return
    translateEnabled.value = next
    writeStoredFlag(next)
    if (next) void translate(templates.value)
  }

  function toggleTranslate(): void {
    setEnabled(!translateEnabled.value)
  }

  /** Re-attempt the cards that previously failed (A2 explicit retry). */
  async function retryFailed(): Promise<void> {
    if (!translateEnabled.value) return
    const failed = new Set(failedIds.value)
    failedIds.value = []
    await translate(templates.value.filter((tpl) => failed.has(tpl.id)))
  }

  // Keep translations in sync when the list reloads (wizard save adds /
  // overwrites rows) — content-signature invalidation handles overwrites.
  watch(
    templates,
    () => {
      if (translateEnabled.value) void translate(templates.value)
    },
    { immediate: true },
  )

  // Target language change (operator switched their language mid-session):
  // old-language cache keys stop matching, stale failures are dropped, and
  // the new language is fetched.
  watch(
    () => normaliseLang(targetLanguage()),
    () => {
      failedIds.value = []
      if (translateEnabled.value) void translate(templates.value)
    },
  )

  if (getCurrentScope()) {
    onScopeDispose(() => {
      disposed = true
    })
  }

  return {
    translateEnabled: computed(() => translateEnabled.value),
    translating,
    hasFailures,
    failedIds: computed(() => failedIds.value),
    needsTranslation,
    displayTemplate,
    toggleTranslate,
    setEnabled,
    retryFailed,
  }
}
