<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CloseOutlined } from '@ant-design/icons-vue'
import { UiButton, UiInput } from '@/components/ui'
import { passwordStrengthLevel } from '@/utils/passwordStrength'

/**
 * The one password prompt behind both halves of the backup panel (CB4).
 *
 * `mode="export"` sets a brand-new one-time password (strength hint,
 * confirmation field, the "this cannot be recovered" warning). `mode="import"`
 * just asks for the password a `.lumebackup` was already encrypted with —
 * no strength hint or confirmation, because the file's password is already
 * whatever it is.
 */
const props = withDefaults(
  defineProps<{
    visible: boolean
    mode: 'export' | 'import'
    loading?: boolean
    /** Already-localized refusal (e.g. wrong password), or null. */
    errorMessage?: string | null
    minLength?: number
  }>(),
  {
    loading: false,
    errorMessage: null,
    minLength: 8,
  },
)

const emit = defineEmits<{
  close: []
  confirm: [password: string]
}>()

const { t } = useI18n()

const password = ref('')
const confirmPassword = ref('')

const strength = computed(() => passwordStrengthLevel(password.value, props.minLength))

const mismatch = computed(() => (
  props.mode === 'export'
  && confirmPassword.value.length > 0
  && password.value !== confirmPassword.value
))

const canSubmit = computed(() => {
  if (props.loading) return false
  if (password.value.length < props.minLength) return false
  if (props.mode === 'export') {
    return confirmPassword.value.length > 0 && password.value === confirmPassword.value
  }
  return true
})

watch(() => props.visible, (visible) => {
  if (visible) {
    window.addEventListener('keydown', handleWindowKeydown)
  } else {
    window.removeEventListener('keydown', handleWindowKeydown)
    password.value = ''
    confirmPassword.value = ''
  }
}, { immediate: true })

onBeforeUnmount(() => {
  window.removeEventListener('keydown', handleWindowKeydown)
})

function handleWindowKeydown(event: KeyboardEvent) {
  if (!props.visible || props.loading) return
  if (event.key === 'Escape') emit('close')
}

function submit() {
  if (!canSubmit.value) return
  emit('confirm', password.value)
}
</script>

<template>
  <Teleport to="body">
    <div
      v-if="visible"
      class="backup-password-modal__backdrop"
      @click.self="!loading && emit('close')"
    >
      <section
        class="backup-password-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="backup-password-modal-title"
      >
        <header class="backup-password-modal__header">
          <h3 id="backup-password-modal-title" class="backup-password-modal__title">
            {{ mode === 'export'
              ? t('characterBackup.password.exportTitle')
              : t('characterBackup.password.importTitle') }}
          </h3>
          <UiButton
            variant="ghost"
            size="sm"
            :disabled="loading"
            :aria-label="t('common.actions.close')"
            @click="emit('close')"
          >
            <CloseOutlined aria-hidden="true" />
          </UiButton>
        </header>

        <p class="backup-password-modal__hint">
          {{ mode === 'export'
            ? t('characterBackup.password.exportHint')
            : t('characterBackup.password.importHint') }}
        </p>

        <p v-if="mode === 'export'" class="backup-password-modal__warning" role="alert">
          {{ t('characterBackup.password.warning') }}
        </p>

        <form class="backup-password-modal__form" @submit.prevent="submit">
          <UiInput
            v-model="password"
            type="password"
            autocomplete="new-password"
            :label="mode === 'export'
              ? t('characterBackup.password.newLabel')
              : t('characterBackup.password.unlockLabel')"
            :disabled="loading"
          />
          <p v-if="mode === 'export'" class="backup-password-modal__strength">
            {{ t(`characterBackup.password.strength.${strength}`) }}
          </p>

          <UiInput
            v-if="mode === 'export'"
            v-model="confirmPassword"
            type="password"
            autocomplete="new-password"
            :label="t('characterBackup.password.confirmLabel')"
            :disabled="loading"
          />
          <p v-if="mismatch" class="backup-password-modal__error" role="alert">
            {{ t('characterBackup.password.mismatch') }}
          </p>

          <p v-if="errorMessage" class="backup-password-modal__error" role="alert">
            {{ errorMessage }}
          </p>

          <div class="backup-password-modal__actions">
            <UiButton
              type="button"
              variant="secondary"
              size="sm"
              :disabled="loading"
              @click="emit('close')"
            >
              {{ t('common.actions.cancel') }}
            </UiButton>
            <UiButton
              type="submit"
              variant="primary"
              size="sm"
              :loading="loading"
              :disabled="!canSubmit"
            >
              {{ mode === 'export'
                ? t('characterBackup.password.exportSubmit')
                : t('characterBackup.password.importSubmit') }}
            </UiButton>
          </div>
        </form>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.backup-password-modal__backdrop {
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
  -webkit-backdrop-filter: blur(5px);
}

.backup-password-modal {
  width: min(420px, calc(100vw - 32px));
  max-height: min(92dvh, 640px);
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

.backup-password-modal__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}

.backup-password-modal__title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-lg);
  font-weight: 650;
}

.backup-password-modal__hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  line-height: 1.6;
}

/* 醒目警語：忘記密碼＝檔案永久無法解開。用警示色而非錯誤色 —— 這句話出現
   在成功路徑上，不是在回報失敗。 */
.backup-password-modal__warning {
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border: 1px solid rgba(255, 209, 128, 0.4);
  border-radius: 6px;
  background: rgba(255, 209, 128, 0.1);
  color: #ffd180;
  font-size: var(--font-xs);
  font-weight: 600;
  line-height: 1.6;
}

.backup-password-modal__form {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.backup-password-modal__strength {
  margin: -4px 0 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.5;
}

.backup-password-modal__error {
  margin: 0;
  color: #f4a3a3;
  font-size: var(--font-xs);
  line-height: 1.5;
}

.backup-password-modal__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  margin-top: var(--space-1);
}
</style>
