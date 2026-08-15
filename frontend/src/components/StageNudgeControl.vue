<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton, UiInput } from '@/components/ui'

/**
 * 示意——讓角色先開口（plan SN §2/§5, ticket SN2）。
 *
 * 一顆輸入列上的小 icon 按鈕：按下彈出可留空的單行輸入＋確認鈕。這個元件
 * 只負責「問玩家要不要補充、補充了什麼」，不知道怎麼送出——實際送出、樂觀
 * 渲染、星號包裹全部留給呼叫端（`ChatPanel` 已經有一份送出管線，`utils/
 * stageNudge.ts` 是兩邊共用的星號規則，這裡不重造任何一個）。
 *
 * 只在同場模式渲染是呼叫端的責任（`v-if="interactionMode === 'stage'"`
 * 包在外面）；這個元件本身對互動模式一無所知，好讓它可以獨立掛測試。
 */
const props = withDefaults(defineProps<{
  characterName: string
  /** 忙碌中（sending／串流中／收回中）—— 圖示按鈕禁用，開著的彈窗也跟著收起。 */
  disabled?: boolean
}>(), {
  disabled: false,
})

const emit = defineEmits<{
  /** 補充文字（已 trim）；留空按下＝空字串，呼叫端據此決定要不要落玩家側訊息。 */
  submit: [text: string]
}>()

const { t } = useI18n()

const open = ref(false)
const text = ref('')
const popoverRef = ref<HTMLElement>()

function toggle() {
  if (props.disabled) return
  open.value = !open.value
  if (open.value) {
    text.value = ''
    void nextTick(() => {
      popoverRef.value?.querySelector('input')?.focus()
    })
  }
}

function close() {
  open.value = false
}

function confirm() {
  if (props.disabled) return
  const value = text.value.trim()
  open.value = false
  text.value = ''
  emit('submit', value)
}

function handleWindowKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

watch(open, (isOpen) => {
  if (isOpen) {
    document.addEventListener('keydown', handleWindowKeydown)
  } else {
    document.removeEventListener('keydown', handleWindowKeydown)
  }
})

// A turn starting elsewhere (Enter in the main textarea, undo) must not
// leave an open popover sitting on top of a now-disabled control.
watch(() => props.disabled, (disabled) => {
  if (disabled) close()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', handleWindowKeydown)
})

// 點外面關閉——與 ChatPanel 的「⋯」選單同一寫法；該檔沒有可匯入的共用元件，
// 這裡沿用同一份實作以保持行為一致。
const vClickOutside = {
  mounted(el: HTMLElement, binding: { value: () => void }) {
    const handler = (event: Event) => {
      if (!el.contains(event.target as Node)) binding.value()
    }
    ;(el as HTMLElement & { _clickOutside?: EventListener })._clickOutside = handler
    document.addEventListener('mousedown', handler)
    document.addEventListener('touchstart', handler, { passive: true })
  },
  unmounted(el: HTMLElement) {
    const handler = (el as HTMLElement & { _clickOutside?: EventListener })._clickOutside
    if (handler) {
      document.removeEventListener('mousedown', handler)
      document.removeEventListener('touchstart', handler)
    }
  },
}
</script>

<template>
  <div v-click-outside="close" class="stage-nudge">
    <button
      type="button"
      class="stage-nudge__trigger"
      :class="{ 'stage-nudge__trigger--open': open }"
      :disabled="disabled"
      :aria-expanded="open"
      aria-haspopup="dialog"
      :title="t('chat.stageNudge.title', { name: characterName })"
      :aria-label="t('chat.stageNudge.title', { name: characterName })"
      @click="toggle"
    >
      <span aria-hidden="true">✦</span>
    </button>

    <div
      v-if="open"
      ref="popoverRef"
      class="stage-nudge__popover"
      role="dialog"
      :aria-label="t('chat.stageNudge.title', { name: characterName })"
    >
      <p class="stage-nudge__title">
        {{ t('chat.stageNudge.title', { name: characterName }) }}
      </p>
      <form class="stage-nudge__form" @submit.prevent="confirm">
        <UiInput
          v-model="text"
          :placeholder="t('chat.stageNudge.placeholder', { name: characterName })"
          :disabled="disabled"
        />
        <div class="stage-nudge__actions">
          <span class="stage-nudge__price"><slot name="price" /></span>
          <UiButton type="submit" variant="primary" size="sm" :disabled="disabled">
            {{ t('chat.stageNudge.confirm') }}
          </UiButton>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.stage-nudge {
  position: relative;
  display: flex;
  align-items: stretch;
  flex: 0 0 auto;
}

.stage-nudge__trigger {
  width: 44px;
  min-width: 44px;
  min-height: 44px;
  background: rgba(255, 255, 255, 0.06);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  color: var(--color-text-secondary);
  font-size: 17px;
  line-height: 1;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s, color 0.15s;
}

.stage-nudge__trigger:hover:not(:disabled),
.stage-nudge__trigger--open {
  background: rgba(255, 255, 255, 0.12);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.stage-nudge__trigger:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.stage-nudge__popover {
  position: absolute;
  right: 0;
  bottom: calc(100% + 8px);
  width: min(320px, calc(100vw - 32px));
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  background: var(--color-bg-secondary, #1f2024);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
  z-index: 25;
}

.stage-nudge__title {
  margin: 0;
  color: var(--color-text);
  font-size: 13px;
  font-weight: 600;
}

.stage-nudge__form {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.stage-nudge__actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.stage-nudge__price {
  min-width: 0;
}

@media (max-width: 720px) {
  .stage-nudge__popover {
    right: auto;
    left: 50%;
    transform: translateX(-50%);
  }
}
</style>
