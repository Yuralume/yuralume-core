<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined } from '@ant-design/icons-vue'
import { UiButton, UiTextarea } from '@/components/ui'
import { publishPlayerPersonaNoteUpdated } from '@/composables/usePlayerPersonaNote'
import {
  PLAYER_PERSONA_NOTE_MAX_CHARS,
  updatePlayerPersonaNote,
} from '@/utils/api/playerPersonaNote'

/**
 * 玩家人設補充的唯一編輯入口（PP4）。
 *
 * 三個地方共用同一顆：進角色聊天的首次彈窗、聊天 header 的常駐 chip、
 * 設定頁的角色分頁。寫入路徑只有這裡一條，所以「存完之後 chip 該不該
 * 變實心」永遠只有一個答案。
 *
 * 版面照 `CharacterBackupPasswordModal` 的既有 overlay 先例（Teleport +
 * backdrop + role="dialog"），沒有另開一套樣式系統。
 */
const props = defineProps<{
  visible: boolean
  characterId: string | null
  characterName: string
  /** 開窗當下已知的內容；由持有 `usePlayerPersonaNote` 的呼叫端提供。 */
  initialNote: string
  /**
   * `initialNote` 是不是「已知事實」（GET 回來過）。
   *
   * 這道閘存在的理由：儲存是整份取代，空字串＝刪除。GET 還在路上或失敗時
   * `initialNote` 是空字串，若照樣給編輯框，玩家看到的是一個空白框——他從
   * 來沒看到自己早就寫過的內容，按下儲存就把它清掉了。所以內容未知時這裡
   * 不給編輯框，只說明狀況並提供重讀。
   */
  noteLoaded: boolean
  /** 那份內容正在讀取中（未知的兩種原因裡，「還沒回來」這一種）。 */
  loading?: boolean
  /**
   * 次要按鈕的文案。不傳時預設 `playerPersonaNote.later`（「之後再說」）——
   * 適合首次彈窗那個語境：按下去等於玩家表態「先不填」，呼叫端會記住這個
   * 選擇（見 `dismiss` emit）。設定頁開的是同一顆 modal，但那裡按下次要
   * 按鈕單純是關窗，不代表任何表態，沿用「之後再說」的文案會誤導玩家以為
   * 有什麼被記住了——呼叫端應改傳既有的 `common.actions.cancel`。
   *
   * 行為本身（emit `dismiss`）不變：改文案只是讓呼叫端說清楚這次按下去
   * 是什麼意思，不新增第二套關窗語意。
   */
  dismissLabel?: string
}>()

const emit = defineEmits<{
  /** 關閉（backdrop／Esc／右上 ×）——不代表玩家表態過。 */
  close: []
  /** 「之後再說」：呼叫端負責記住（首次彈窗才需要）。 */
  dismiss: []
  /** 存檔成功，帶回後端回寫後的內容（空字串＝已清除）。 */
  saved: [note: string]
  /** 內容讀不到時的重試；呼叫端重跑一次 GET。 */
  reload: []
}>()

const { t } = useI18n()

const text = ref('')
const saving = ref(false)
const errorMessage = ref<string | null>(null)

const maxChars = PLAYER_PERSONA_NOTE_MAX_CHARS
const counterText = computed(() => t('playerPersonaNote.counter', {
  used: text.value.length,
  max: maxChars,
}))

// `immediate` 讓「一開就是開著」的掛法也拿得到內容；window 的存在因此不
// 能假設（SSR / 測試環境沒有 window，這個 watcher 會在那裡先跑一次）。
//
// 也看 `noteLoaded`：開窗當下內容還沒回來時編輯框是鎖的，等 GET 落地才把
// 真正的內容灌進去。「未知 → 已知」這個轉折不補灌，玩家就會對著一個永遠
// 空白的框編輯剛剛才讀回來的內容。
watch(() => [props.visible, props.noteLoaded] as const, ([visible, known], prev) => {
  if (!visible) {
    unbindEscape()
    return
  }
  const wasVisible = prev?.[0] ?? false
  const wasKnown = prev?.[1] ?? false
  if (!wasVisible) {
    errorMessage.value = null
    bindEscape()
  }
  // 內容未知時編輯框不存在，所以這裡不可能蓋掉玩家打到一半的字。
  if (known && (!wasVisible || !wasKnown)) text.value = props.initialNote
}, { immediate: true })

onBeforeUnmount(unbindEscape)

function bindEscape() {
  if (typeof window === 'undefined') return
  window.addEventListener('keydown', handleWindowKeydown)
}

function unbindEscape() {
  if (typeof window === 'undefined') return
  window.removeEventListener('keydown', handleWindowKeydown)
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (!props.visible || saving.value) return
  if (event.key === 'Escape') emit('close')
}

async function save() {
  if (saving.value || !props.characterId) return
  // 沒讀到現況就不准寫：整份取代 + 空白框＝無聲清除既有自述。
  if (!props.noteLoaded) return
  saving.value = true
  errorMessage.value = null
  try {
    // 空字串是有意義的值（清除），所以照送不做 falsy 短路。
    const result = await updatePlayerPersonaNote(props.characterId, text.value.trim())
    const stored = result.note ?? ''
    emit('saved', stored)
    // 廣播給其他掛著同一個角色的 surface（聊天 chip ↔ 設定頁欄位）。寫入
    // 路徑只有這一條，所以廣播也只在這裡發一次。
    publishPlayerPersonaNoteUpdated(props.characterId, stored)
    emit('close')
  } catch (err) {
    // 失敗不關窗——玩家剛打的字還在框裡，關掉就丟了。
    errorMessage.value = err instanceof Error
      ? t('common.errorWithDetail', {
        message: t('playerPersonaNote.saveFailed'),
        detail: err.message,
      })
      : t('playerPersonaNote.saveFailed')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="persona-note-modal__backdrop"
      @click.self="!saving && emit('close')"
    >
      <section
        class="persona-note-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="player-persona-note-modal-title"
      >
        <header class="persona-note-modal__header">
          <h3 id="player-persona-note-modal-title" class="persona-note-modal__title">
            {{ t('playerPersonaNote.title', { name: characterName }) }}
          </h3>
          <UiButton
            variant="ghost"
            size="sm"
            :disabled="saving"
            :aria-label="t('common.actions.close')"
            @click="emit('close')"
          >
            <CloseOutlined aria-hidden="true" />
          </UiButton>
        </header>

        <p class="persona-note-modal__hint">
          {{ t('playerPersonaNote.description', { name: characterName }) }}
        </p>

        <!-- 內容未知：不給編輯框。空白框 + 整份取代＝按下儲存就把玩家從
             來沒看到的既有自述清掉。 -->
        <div v-if="!noteLoaded" class="persona-note-modal__form">
          <p v-if="loading" class="persona-note-modal__hint">
            {{ t('playerPersonaNote.loading') }}
          </p>
          <template v-else>
            <p class="persona-note-modal__error" role="alert">
              {{ t('playerPersonaNote.loadFailed') }}
            </p>
            <div class="persona-note-modal__actions">
              <UiButton
                type="button"
                variant="primary"
                size="sm"
                @click="emit('reload')"
              >
                {{ t('common.actions.retry') }}
              </UiButton>
            </div>
          </template>
        </div>

        <form v-else class="persona-note-modal__form" @submit.prevent="save">
          <UiTextarea
            v-model="text"
            :label="t('playerPersonaNote.fieldLabel')"
            :placeholder="t('playerPersonaNote.placeholder')"
            :rows="5"
            :maxlength="maxChars"
            :disabled="saving"
          />
          <p class="persona-note-modal__counter">{{ counterText }}</p>
          <p class="persona-note-modal__hint">{{ t('playerPersonaNote.clearHint') }}</p>

          <p v-if="errorMessage" class="persona-note-modal__error" role="alert">
            {{ errorMessage }}
          </p>

          <div class="persona-note-modal__actions">
            <UiButton
              type="button"
              variant="secondary"
              size="sm"
              :disabled="saving"
              @click="emit('dismiss')"
            >
              {{ dismissLabel ?? t('playerPersonaNote.later') }}
            </UiButton>
            <UiButton
              type="submit"
              variant="primary"
              size="sm"
              :loading="saving"
              :disabled="!characterId"
            >
              {{ t('playerPersonaNote.save') }}
            </UiButton>
          </div>
        </form>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.persona-note-modal__backdrop {
  position: fixed;
  inset: 0;
  height: 100dvh;
  z-index: 920;
  display: flex;
  align-items: center;
  justify-content: center;
  padding:
    max(24px, var(--safe-area-top))
    max(24px, var(--safe-area-right))
    max(24px, var(--safe-area-bottom))
    max(24px, var(--safe-area-left));
  background: rgba(0, 0, 0, 0.62);
  backdrop-filter: blur(5px);
}

.persona-note-modal {
  width: min(460px, calc(100vw - 32px));
  max-height: min(92dvh, 680px);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  border: 1px solid rgba(64, 156, 255, 0.34);
  border-radius: 8px;
  padding: var(--space-4);
  background: rgba(24, 33, 50, 0.98);
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.46);
  overflow-y: auto;
}

.persona-note-modal__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.persona-note-modal__title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-lg);
  font-weight: 650;
}

.persona-note-modal__hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  line-height: 1.6;
}

.persona-note-modal__form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.persona-note-modal__counter {
  margin: -4px 0 0;
  align-self: flex-end;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.5;
}

.persona-note-modal__error {
  margin: 0;
  color: #f4a3a3;
  font-size: var(--font-xs);
  line-height: 1.5;
}

.persona-note-modal__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-1);
}
</style>
