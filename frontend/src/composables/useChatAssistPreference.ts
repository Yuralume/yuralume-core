import { readonly, ref } from 'vue'
import {
  getChatAssistPreference,
  setChatAssistPreference,
} from '@/utils/api/system'

const enabled = ref(true)
const loading = ref(false)
const loaded = ref(false)

async function loadChatAssistPreference(force = false): Promise<boolean> {
  if (loaded.value && !force) return enabled.value
  loading.value = true
  try {
    const pref = await getChatAssistPreference()
    enabled.value = pref.enabled
    loaded.value = true
  } catch {
    enabled.value = true
  } finally {
    loading.value = false
  }
  return enabled.value
}

async function saveChatAssistPreference(next: boolean): Promise<boolean> {
  const pref = await setChatAssistPreference({ enabled: next })
  enabled.value = pref.enabled
  loaded.value = true
  return enabled.value
}

export function useChatAssistPreference() {
  return {
    chatAssistEnabled: enabled,
    chatAssistLoading: readonly(loading),
    loadChatAssistPreference,
    saveChatAssistPreference,
  }
}
