<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import { UiButton } from '@/components/ui'
import PlayerPersonaNoteModal from './PlayerPersonaNoteModal.vue'
import { usePlayerPersonaNote } from '@/composables/usePlayerPersonaNote'

/**
 * 設定頁「選一位角色再調整」分頁裡的玩家人設欄位（PP4）。
 *
 * 這裡刻意不自己長一個 textarea：編輯一律走 `PlayerPersonaNoteModal`，
 * 跟聊天面板同一顆。設定頁只負責「現在填了什麼」的呈現與入口。
 */
const props = defineProps<{
  character: Character
}>()

const { t } = useI18n()

const { note, loaded, loading, load, reload, apply } = usePlayerPersonaNote()
const modalOpen = ref(false)
const savedFeedback = ref(false)

const hasNote = computed(() => note.value.trim().length > 0)
/** 讀不到現況：既不是「載入中」也不是「還沒填」，不能拿其中任一個充數。 */
const loadFailed = computed(() => !loading.value && !loaded.value)

watch(() => props.character.id, (characterId) => {
  savedFeedback.value = false
  modalOpen.value = false
  void load(characterId)
}, { immediate: true })

function openEditor() {
  savedFeedback.value = false
  // 上一次沒讀成功就先重試；在它回來之前 modal 只會說明狀況，不給編輯框。
  if (loadFailed.value) void reload()
  modalOpen.value = true
}

function handleSaved(value: string) {
  apply(value)
  savedFeedback.value = true
}
</script>

<template>
  <div class="persona-note-setting">
    <p class="persona-note-setting__hint">
      {{ t('playerPersonaNote.description', { name: character.name }) }}
    </p>

    <p v-if="loading" class="persona-note-setting__hint">
      {{ t('playerPersonaNote.loading') }}
    </p>
    <!-- 讀失敗時不准說「還沒有填寫」——那句話會讓玩家以為按下去是新增，
         實際上他早就填過，而空白編輯框的儲存是整份取代。 -->
    <p v-else-if="loadFailed" class="persona-note-setting__failed" role="alert">
      {{ t('playerPersonaNote.loadFailed') }}
    </p>
    <p v-else-if="hasNote" class="persona-note-setting__value">{{ note }}</p>
    <p v-else class="persona-note-setting__hint">{{ t('playerPersonaNote.empty') }}</p>

    <div class="persona-note-setting__actions">
      <UiButton variant="primary" size="sm" :disabled="loading" @click="openEditor">
        {{ loadFailed ? t('common.actions.retry') : (hasNote ? t('playerPersonaNote.edit') : t('playerPersonaNote.add')) }}
      </UiButton>
    </div>

    <p v-if="savedFeedback" class="persona-note-setting__feedback">
      {{ t('playerPersonaNote.saved') }}
    </p>

    <PlayerPersonaNoteModal
      :visible="modalOpen"
      :character-id="character.id"
      :character-name="character.name"
      :initial-note="note"
      :note-loaded="loaded"
      :loading="loading"
      :dismiss-label="t('common.actions.cancel')"
      @close="modalOpen = false"
      @dismiss="modalOpen = false"
      @saved="handleSaved"
      @reload="reload"
    />
  </div>
</template>

<style scoped>
.persona-note-setting {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.persona-note-setting__hint,
.persona-note-setting__feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.persona-note-setting__feedback {
  color: #7dc49a;
}

.persona-note-setting__failed {
  margin: 0;
  color: #f4a3a3;
  font-size: 11px;
  line-height: 1.45;
}

.persona-note-setting__value {
  margin: 0;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-size: var(--font-xs);
  line-height: 1.6;
  white-space: pre-wrap;
}
</style>
