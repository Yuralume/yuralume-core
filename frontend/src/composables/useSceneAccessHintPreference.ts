import { readonly, ref } from 'vue'
import {
  getSceneAccessHintPreference,
  setSceneAccessHintPreference,
} from '@/utils/api/system'

const enabled = ref(true)
const loading = ref(false)
const loaded = ref(false)

async function loadSceneAccessHintPreference(force = false): Promise<boolean> {
  if (loaded.value && !force) return enabled.value
  loading.value = true
  try {
    const pref = await getSceneAccessHintPreference()
    enabled.value = pref.enabled
    loaded.value = true
  } catch {
    enabled.value = true
  } finally {
    loading.value = false
  }
  return enabled.value
}

async function saveSceneAccessHintPreference(next: boolean): Promise<boolean> {
  const pref = await setSceneAccessHintPreference({ enabled: next })
  enabled.value = pref.enabled
  loaded.value = true
  return enabled.value
}

export function useSceneAccessHintPreference() {
  return {
    sceneAccessHintEnabled: enabled,
    sceneAccessHintLoading: readonly(loading),
    loadSceneAccessHintPreference,
    saveSceneAccessHintPreference,
  }
}
