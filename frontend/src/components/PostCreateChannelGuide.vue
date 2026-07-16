<script setup lang="ts">
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton } from '@/components/ui'
import type { Character } from '@/types/character'
import type { MessagingPlatform } from '@/types/messaging'
import { listAccounts } from '@/utils/api/messaging'

const props = defineProps<{ character: Character | null }>()

const emit = defineEmits<{
  setup: [platform: MessagingPlatform]
  dismiss: []
}>()

const { t } = useI18n()

const visible = ref(false)
const loading = ref(false)

watch(() => props.character?.id, () => {
  void evaluateVisibility()
}, { immediate: true })

function storageKey(characterId: string): string {
  return `yuralume.channelGuide.skipped.${characterId}`
}

function wasDismissed(characterId: string): boolean {
  if (typeof window === 'undefined') return false
  try {
    return localStorage.getItem(storageKey(characterId)) === '1'
  } catch {
    return false
  }
}

function rememberDismissed(characterId: string) {
  if (typeof window === 'undefined') return
  try {
    localStorage.setItem(storageKey(characterId), '1')
  } catch {
    /* localStorage can be unavailable in private or embedded contexts. */
  }
}

async function evaluateVisibility() {
  const character = props.character
  visible.value = false
  if (!character || wasDismissed(character.id)) return
  loading.value = true
  try {
    const accounts = await listAccounts(character.id)
    visible.value = accounts.length === 0
  } catch {
    visible.value = true
  } finally {
    loading.value = false
  }
}

function choosePlatform(platform: MessagingPlatform) {
  if (props.character) rememberDismissed(props.character.id)
  visible.value = false
  emit('setup', platform)
}

function dismiss() {
  if (props.character) rememberDismissed(props.character.id)
  visible.value = false
  emit('dismiss')
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible && character"
      class="post-create-channel-guide__backdrop"
      @click.self="dismiss"
    >
      <section
        class="post-create-channel-guide"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="`post-create-channel-guide-title-${character.id}`"
      >
        <button
          type="button"
          class="post-create-channel-guide__close"
          :aria-label="t('postCreateChannelGuide.actions.later')"
          :disabled="loading"
          @click="dismiss"
        >
          ×
        </button>

        <div class="post-create-channel-guide__copy">
          <h3
            :id="`post-create-channel-guide-title-${character.id}`"
            class="post-create-channel-guide__title"
          >
            {{ t('postCreateChannelGuide.title', { name: character.name }) }}
          </h3>
          <p class="post-create-channel-guide__hint">
            {{ t('postCreateChannelGuide.hint') }}
          </p>
        </div>

        <div class="post-create-channel-guide__actions">
          <UiButton
            variant="primary"
            size="md"
            :disabled="loading"
            @click="choosePlatform('telegram')"
          >{{ t('postCreateChannelGuide.actions.telegram') }}</UiButton>
          <UiButton
            size="md"
            :disabled="loading"
            @click="choosePlatform('line')"
          >{{ t('postCreateChannelGuide.actions.line') }}</UiButton>
          <UiButton
            size="md"
            :disabled="loading"
            @click="choosePlatform('discord')"
          >{{ t('postCreateChannelGuide.actions.discord') }}</UiButton>
          <UiButton
            size="md"
            :disabled="loading"
            @click="choosePlatform('whatsapp')"
          >{{ t('postCreateChannelGuide.actions.whatsapp') }}</UiButton>
          <button
            type="button"
            class="post-create-channel-guide__skip"
            :disabled="loading"
            @click="dismiss"
          >
            {{ t('postCreateChannelGuide.actions.later') }}
          </button>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.post-create-channel-guide__backdrop {
  position: fixed;
  inset: 0;
  z-index: 900;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  background: rgba(0, 0, 0, 0.58);
  backdrop-filter: blur(5px);
  -webkit-backdrop-filter: blur(5px);
}

.post-create-channel-guide {
  position: relative;
  width: min(440px, calc(100vw - 32px));
  border: 1px solid rgba(64, 156, 255, 0.38);
  border-radius: 8px;
  padding: 22px;
  background: rgba(24, 33, 50, 0.98);
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.46);
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.post-create-channel-guide__close {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 28px;
  height: 28px;
  border: 0;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
  cursor: pointer;
  font: inherit;
  font-size: 18px;
  line-height: 1;
}

.post-create-channel-guide__close:hover:not(:disabled) {
  color: var(--color-text);
  background: rgba(255, 255, 255, 0.13);
}

.post-create-channel-guide__copy {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-right: 20px;
}

.post-create-channel-guide__title {
  margin: 0;
  font-size: 18px;
  line-height: 1.35;
  font-weight: 650;
  color: var(--color-text);
}

.post-create-channel-guide__hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.65;
}

.post-create-channel-guide__actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.post-create-channel-guide__skip {
  border: 0;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font: inherit;
  font-size: 13px;
  padding: 6px 2px;
}

.post-create-channel-guide__skip:hover:not(:disabled) {
  color: var(--color-text);
  text-decoration: underline;
}

.post-create-channel-guide__skip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 480px) {
  .post-create-channel-guide__backdrop {
    align-items: flex-end;
    padding: 16px;
  }

  .post-create-channel-guide {
    width: 100%;
    padding: 20px;
  }
}
</style>
