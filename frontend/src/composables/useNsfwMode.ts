import { computed, ref } from 'vue'
import {
  getNsfwModePreference,
  setNsfwModePreference,
  type NsfwModePreference,
} from '@/utils/api/system'

const status = ref<NsfwModePreference | null>(null)
const loading = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)
const now = ref(Date.now())
let clock: ReturnType<typeof setInterval> | null = null
let clockUsers = 0

const active = computed(() => status.value?.active === true)
const configured = computed(() => status.value?.configured === true)
const locked = computed(() => status.value?.locked === true)
const target = computed(() => status.value?.target ?? null)

const remainingSeconds = computed(() => {
  const expiresAt = status.value?.expires_at
  if (!active.value || !expiresAt) return null
  const expiresMs = Date.parse(expiresAt)
  if (!Number.isFinite(expiresMs)) return null
  return Math.max(0, Math.ceil((expiresMs - now.value) / 1000))
})

function startNsfwModeClock(): void {
  clockUsers += 1
  now.value = Date.now()
  if (clock) return
  clock = setInterval(() => {
    now.value = Date.now()
  }, 30_000)
}

function stopNsfwModeClock(): void {
  clockUsers = Math.max(0, clockUsers - 1)
  if (clockUsers > 0 || !clock) return
  clearInterval(clock)
  clock = null
}

async function loadNsfwMode(): Promise<NsfwModePreference | null> {
  loading.value = true
  error.value = null
  try {
    status.value = await getNsfwModePreference()
    now.value = Date.now()
    return status.value
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    return null
  } finally {
    loading.value = false
  }
}

async function enableNsfwMode(): Promise<NsfwModePreference> {
  saving.value = true
  error.value = null
  try {
    status.value = await setNsfwModePreference({ active: true })
    now.value = Date.now()
    return status.value
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    throw err
  } finally {
    saving.value = false
  }
}

async function disableNsfwMode(): Promise<NsfwModePreference> {
  saving.value = true
  error.value = null
  try {
    status.value = await setNsfwModePreference({ active: false })
    now.value = Date.now()
    return status.value
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err)
    throw err
  } finally {
    saving.value = false
  }
}

export function useNsfwMode() {
  return {
    status,
    loading,
    saving,
    error,
    active,
    configured,
    locked,
    target,
    remainingSeconds,
    loadNsfwMode,
    enableNsfwMode,
    disableNsfwMode,
    startNsfwModeClock,
    stopNsfwModeClock,
  }
}
