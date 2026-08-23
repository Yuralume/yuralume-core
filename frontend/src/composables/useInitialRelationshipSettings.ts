import { computed, ref } from 'vue'
import {
  getInitialRelationship,
  updateInitialRelationship,
  type InitialRelationshipSeed,
} from '@/utils/api/initialRelationship'
import {
  buildInitialRelationshipEditPatch,
  emptyInitialRelationshipEditForm,
  type InitialRelationshipEditForm,
} from './useInitialRelationshipForm'

/**
 * 設定頁「關係與相處設定」編輯器的載入／儲存狀態（IR2）。
 *
 * 呈現與翻譯都留給元件；這裡只管兩件事：把後端的整份 seed 轉成表單、以
 * 及在儲存時只送真的改過的欄位（tri-state，見
 * `buildInitialRelationshipEditPatch`）。`original` 是上一次成功
 * GET／PATCH 拿到的值，儲存時拿它跟目前表單做差集——不是拿「表單初始值」
 * 做差集，所以連續存兩次、第二次只改一個欄位時，第一次已經存過的欄位不
 * 會被誤判成「又改了」而重送一次空字串。
 */
function toEditForm(seed: InitialRelationshipSeed): InitialRelationshipEditForm {
  return {
    relationship_label: seed.relationship_label,
    known_context: seed.known_context,
    living_arrangement: seed.living_arrangement,
    user_address_name: seed.user_address_name,
    character_address_name: seed.character_address_name,
    tone_distance: seed.tone_distance,
    familiarity_boundary: seed.familiarity_boundary,
    schedule_involvement_policy: seed.schedule_involvement_policy,
    proactive_permission: seed.proactive_permission,
    proactive_cadence_hint: seed.proactive_cadence_hint,
    user_profile_notes: seed.user_profile_notes,
  }
}

export function useInitialRelationshipSettings() {
  const form = ref<InitialRelationshipEditForm>(emptyInitialRelationshipEditForm())
  const original = ref<InitialRelationshipEditForm>(emptyInitialRelationshipEditForm())
  const loading = ref(false)
  /** GET 成功回來過一次。載入中、失敗都是 false。 */
  const loaded = ref(false)
  const saving = ref(false)

  /** 有沒有真的改過任何欄位——存檔按鈕看這個決定要不要能按。 */
  const hasChanges = computed(() => (
    buildInitialRelationshipEditPatch(original.value, form.value) !== null
  ))

  // 切角色時舊請求可能還在路上；回來得晚的那一個不准覆寫新角色的表單。
  let requestSeq = 0
  let currentCharacterId: string | null = null

  async function load(characterId: string): Promise<void> {
    const seq = ++requestSeq
    currentCharacterId = characterId
    loading.value = true
    loaded.value = false
    try {
      const seed = await getInitialRelationship(characterId)
      if (seq !== requestSeq) return
      const editForm = toEditForm(seed)
      original.value = editForm
      form.value = { ...editForm }
      loaded.value = true
    } catch {
      // Fail-soft，同 `usePlayerPersonaNote`：讀不到就停在「不知道」，
      // `loaded` 留 false。元件的 `watch(..., { immediate: true })` 用
      // `void load(...)` 呼叫、不接錯誤，這裡不吞掉會變成未處理的
      // rejection。
      if (seq !== requestSeq) return
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  /**
   * 存檔。沒有任何欄位真的改過時直接回報「沒東西要存」，不呼叫 API——
   * 也就不會把未動過的欄位送成空字串。
   */
  async function save(): Promise<{ changed: boolean }> {
    if (!currentCharacterId) return { changed: false }
    const patch = buildInitialRelationshipEditPatch(original.value, form.value)
    if (!patch) return { changed: false }

    saving.value = true
    try {
      const seed = await updateInitialRelationship(currentCharacterId, patch)
      const editForm = toEditForm(seed)
      original.value = editForm
      form.value = { ...editForm }
      return { changed: true }
    } finally {
      saving.value = false
    }
  }

  return { form, original, loading, loaded, saving, hasChanges, load, save }
}
