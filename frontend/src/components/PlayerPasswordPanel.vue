<script setup lang="ts">
import { computed, ref } from 'vue'
import axios from 'axios'
import { useI18n } from 'vue-i18n'
import { changeOwnPassword } from '@/utils/api/auth'
import { UiButton } from '@/components/ui'

withDefaults(defineProps<{
  showTitle?: boolean
}>(), {
  showTitle: true,
})

const { t } = useI18n()

const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const saving = ref(false)
const errorMsg = ref<string | null>(null)
const successMsg = ref<string | null>(null)

const canSubmit = computed(() => (
  Boolean(currentPassword.value.trim()) &&
  Boolean(newPassword.value.trim()) &&
  Boolean(confirmPassword.value.trim()) &&
  !saving.value
))

function resetForm() {
  currentPassword.value = ''
  newPassword.value = ''
  confirmPassword.value = ''
}

function passwordErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error) && error.response?.status === 400) {
    return t('playerSidebar.password.currentPasswordIncorrect')
  }
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail
    if (typeof detail === 'string' && detail.trim()) return detail
  }
  return error instanceof Error && error.message
    ? error.message
    : t('playerSidebar.password.saveFailed')
}

async function submitPasswordChange() {
  errorMsg.value = null
  successMsg.value = null
  if (!currentPassword.value.trim() || !newPassword.value.trim() || !confirmPassword.value.trim()) {
    errorMsg.value = t('playerSidebar.password.required')
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    errorMsg.value = t('playerSidebar.password.mismatch')
    return
  }
  saving.value = true
  try {
    await changeOwnPassword(currentPassword.value, newPassword.value)
    resetForm()
    successMsg.value = t('playerSidebar.password.saved')
  } catch (error) {
    errorMsg.value = passwordErrorMessage(error)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="player-password-panel">
    <div v-if="showTitle" class="player-password-panel__header">
      <h4>{{ t('playerSidebar.password.title') }}</h4>
      <p>{{ t('playerSidebar.password.hint') }}</p>
    </div>
    <p v-else class="player-password-panel__hint">
      {{ t('playerSidebar.password.hint') }}
    </p>
    <form class="player-password-panel__form" @submit.prevent="submitPasswordChange">
      <label class="field-label" for="player-current-password">
        {{ t('playerSidebar.password.currentLabel') }}
      </label>
      <input
        id="player-current-password"
        v-model="currentPassword"
        class="field-input"
        type="password"
        autocomplete="current-password"
        :disabled="saving"
      />

      <label class="field-label" for="player-new-password">
        {{ t('playerSidebar.password.newLabel') }}
      </label>
      <input
        id="player-new-password"
        v-model="newPassword"
        class="field-input"
        type="password"
        autocomplete="new-password"
        :disabled="saving"
      />

      <label class="field-label" for="player-confirm-password">
        {{ t('playerSidebar.password.confirmLabel') }}
      </label>
      <input
        id="player-confirm-password"
        v-model="confirmPassword"
        class="field-input"
        type="password"
        autocomplete="new-password"
        :disabled="saving"
      />

      <div class="player-password-panel__actions">
        <UiButton
          type="submit"
          variant="primary"
          size="sm"
          :loading="saving"
          :disabled="!canSubmit"
        >
          {{ t('playerSidebar.password.saveAction') }}
        </UiButton>
        <UiButton
          type="button"
          variant="ghost"
          size="sm"
          :disabled="saving"
          @click="resetForm"
        >
          {{ t('common.actions.clear') }}
        </UiButton>
      </div>
      <p v-if="errorMsg" class="player-password-panel__message error" role="alert">
        {{ errorMsg }}
      </p>
      <p v-if="successMsg" class="player-password-panel__message success" role="status">
        {{ successMsg }}
      </p>
    </form>
  </section>
</template>

<style scoped>
.player-password-panel {
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.player-password-panel__header h4 {
  margin: 0 0 3px;
  color: var(--color-text);
  font-size: 13px;
  font-weight: 600;
}

.player-password-panel__header p {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.player-password-panel__hint {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.player-password-panel__form {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.player-password-panel__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 2px;
}

.player-password-panel__message {
  margin: 0;
  font-size: 11px;
  line-height: 1.45;
}

.player-password-panel__message.error {
  color: #ff8a75;
}

.player-password-panel__message.success {
  color: #7dc49a;
}
</style>
