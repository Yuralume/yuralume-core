<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'

/**
 * 聊天 header 的「你的人設」常駐入口（PP4）。
 *
 * 只負責「顯示填了沒 + 按下去」；開哪一顆 modal、內容是什麼都不知道，
 * 好讓已填／未填兩種外觀可以獨立掛測試。
 *
 * 已填＝實心（`active`）＋實心圓點；未填＝空心 chip ＋空心圓點。顏色以外
 * 再給一個字符差異，是因為 chip 的兩種狀態在深色底上只差一層底色，單靠
 * 顏色分辨對低對比環境與色覺差異的人不成立。
 */
const props = defineProps<{
  /** 這個角色已經有非空的玩家自述。 */
  filled: boolean
  characterName: string
  disabled?: boolean
}>()

const emit = defineEmits<{ open: [] }>()

const { t } = useI18n()

const description = computed(() => (
  props.filled
    ? t('playerPersonaNote.chip.filledHint', { name: props.characterName })
    : t('playerPersonaNote.chip.emptyHint', { name: props.characterName })
))
</script>

<template>
  <UiButton
    variant="chip"
    size="sm"
    class="persona-note-chip"
    :active="filled"
    :disabled="disabled"
    :title="description"
    :aria-label="description"
    @click="emit('open')"
  >
    <span class="persona-note-chip__dot" aria-hidden="true">{{ filled ? '●' : '○' }}</span>
    <span>{{ t('playerPersonaNote.chip.label') }}</span>
  </UiButton>
</template>

<style scoped>
.persona-note-chip {
  white-space: nowrap;
}

.persona-note-chip__dot {
  margin-right: 4px;
  font-size: 9px;
  line-height: 1;
}
</style>
