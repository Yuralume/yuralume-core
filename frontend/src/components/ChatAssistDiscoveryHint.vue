<script setup lang="ts">
import { BulbOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'

defineProps<{
  visible: boolean
  characterName: string
}>()

const emit = defineEmits<{
  open: []
  dismiss: []
}>()

const { t } = useI18n()
</script>

<template>
  <Transition name="chat-assist-discovery">
    <div
      v-if="visible"
      class="chat-assist-discovery"
      role="note"
    >
      <button
        type="button"
        class="chat-assist-discovery__main"
        @click="emit('open')"
      >
        <BulbOutlined class="chat-assist-discovery__icon" aria-hidden="true" />
        <span class="chat-assist-discovery__text">
          {{ t('chat.assist.discover.prompt', { name: characterName }) }}
        </span>
      </button>
      <UiButton
        variant="ghost"
        size="sm"
        class="chat-assist-discovery__dismiss"
        :aria-label="t('chat.assist.discover.dismiss')"
        @click.stop="emit('dismiss')"
      >
        {{ t('chat.assist.discover.dismiss') }}
      </UiButton>
    </div>
  </Transition>
</template>

<style scoped>
.chat-assist-discovery {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 8px;
  border: 1px solid rgba(106, 169, 240, 0.28);
  border-radius: 8px;
  background: rgba(106, 169, 240, 0.08);
}

.chat-assist-discovery__main {
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  flex: 1 1 auto;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--color-text);
  font: inherit;
  font-size: 12px;
  line-height: 1.4;
  text-align: left;
  cursor: pointer;
}

.chat-assist-discovery__main:hover {
  color: var(--color-primary);
}

.chat-assist-discovery__icon {
  flex: 0 0 auto;
  color: var(--color-primary);
}

.chat-assist-discovery__text {
  min-width: 0;
  overflow-wrap: anywhere;
}

.chat-assist-discovery__dismiss {
  flex: 0 0 auto;
}

.chat-assist-discovery-enter-active,
.chat-assist-discovery-leave-active {
  transition: opacity 0.16s ease, transform 0.16s ease;
}

.chat-assist-discovery-enter-from,
.chat-assist-discovery-leave-to {
  opacity: 0;
  transform: translateY(4px);
}
</style>
