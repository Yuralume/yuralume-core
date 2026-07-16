<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import {
  attachExistingLora,
  listAvailableLoras,
  removeCharacterLora,
  setCharacterLoraStrength,
  uploadCharacterLora,
} from '@/utils/api/characters'
import { UiButton } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'

const props = defineProps<{
  character: Character
}>()

const emit = defineEmits<{
  updated: [char: Character]
}>()

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const uploading = ref(false)
const busyName = ref<string | null>(null)
const errorMsg = ref<string | null>(null)
const available = ref<string[]>([])
const attachName = ref('')
const attachStrength = ref(1.0)

async function refreshAvailable() {
  try {
    available.value = await listAvailableLoras(props.character.id)
  } catch {
    available.value = []
  }
}

onMounted(refreshAvailable)
watch(() => props.character.id, refreshAvailable)

async function handleFilePick(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  input.value = ''

  uploading.value = true
  errorMsg.value = null
  try {
    const updated = await uploadCharacterLora(props.character.id, file, 1.0)
    emit('updated', updated)
    await refreshAvailable()
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterLorasPanel.errors.uploadFailed')
  } finally {
    uploading.value = false
  }
}

async function handleAttach() {
  const name = attachName.value.trim()
  if (!name) return
  busyName.value = name
  errorMsg.value = null
  try {
    const updated = await attachExistingLora(
      props.character.id, name, attachStrength.value,
    )
    emit('updated', updated)
    attachName.value = ''
    attachStrength.value = 1.0
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterLorasPanel.errors.attachFailed')
  } finally {
    busyName.value = null
  }
}

async function handleStrengthChange(name: string, strength: number) {
  busyName.value = name
  errorMsg.value = null
  try {
    const updated = await setCharacterLoraStrength(
      props.character.id, name, strength,
    )
    emit('updated', updated)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterLorasPanel.errors.strengthFailed')
  } finally {
    busyName.value = null
  }
}

async function handleRemove(name: string) {
  if (!await confirmDialog({
    content: t('characterLorasPanel.confirm.remove', { name }),
    okText: t('common.actions.remove'),
    danger: true,
  })) {
    return
  }
  busyName.value = name
  errorMsg.value = null
  try {
    const updated = await removeCharacterLora(props.character.id, name)
    emit('updated', updated)
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('characterLorasPanel.errors.removeFailed')
  } finally {
    busyName.value = null
  }
}

function extractError(err: unknown): string | null {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: { detail?: string } } }).response
    if (resp?.data?.detail) return resp.data.detail
  }
  return err instanceof Error ? err.message : null
}
</script>

<template>
  <div class="loras-panel">
    <div class="loras-header">
      <h3 class="section-title">{{ t('characterLorasPanel.title') }}</h3>
      <p class="field-hint">
        {{ t('characterLorasPanel.hintPrefix') }}
        <code>models/loras/</code>
        {{ t('characterLorasPanel.hintMiddle') }}
        <code>KOKORO_COMFYUI_LORA_DIR</code>
        {{ t('characterLorasPanel.hintSuffix') }}
      </p>
    </div>

    <div v-if="character.loras.length === 0" class="loras-empty">
      {{ t('characterLorasPanel.emptyPrefix') }} <code>.safetensors</code> {{ t('characterLorasPanel.emptySuffix') }}
    </div>
    <div v-else class="loras-list">
      <div
        v-for="lora in character.loras"
        :key="lora.name"
        class="lora-row"
      >
        <div class="lora-name" :title="lora.name">{{ lora.name }}</div>
        <div class="lora-controls">
          <label class="field-label lora-strength-label">{{ t('characterLorasPanel.strengthLabel') }}</label>
          <input
            type="range"
            min="0"
            max="2"
            step="0.05"
            :value="lora.strength"
            class="field-range"
            :disabled="busyName === lora.name"
            @change="handleStrengthChange(lora.name, Number(($event.target as HTMLInputElement).value))"
          />
          <span class="range-value">{{ lora.strength.toFixed(2) }}</span>
          <button
            class="btn-icon btn-icon-danger"
            :title="t('common.actions.remove')"
            :disabled="busyName === lora.name"
            @click="handleRemove(lora.name)"
          >×</button>
        </div>
      </div>
    </div>

    <label :class="['upload-btn', { disabled: uploading }]">
      <input
        type="file"
        accept=".safetensors,.ckpt,.pt"
        :disabled="uploading"
        @change="handleFilePick"
      />
      <span>{{ uploading ? t('characterLorasPanel.actions.uploading') : t('characterLorasPanel.actions.upload') }}</span>
    </label>

    <div v-if="available.length > 0" class="attach-section">
      <label class="field-label">{{ t('characterLorasPanel.attach.label') }}</label>
      <div class="attach-row">
        <select v-model="attachName" class="field-select">
          <option value="">{{ t('characterLorasPanel.attach.placeholder') }}</option>
          <option v-for="name in available" :key="name" :value="name">{{ name }}</option>
        </select>
        <input
          v-model.number="attachStrength"
          type="number"
          min="0"
          max="2"
          step="0.1"
          class="field-input strength-input"
        />
        <UiButton
          size="sm"
          :disabled="!attachName || busyName === attachName"
          @click="handleAttach"
        >{{ t('characterLorasPanel.attach.action') }}</UiButton>
      </div>
    </div>

    <div v-if="errorMsg" class="loras-error">{{ errorMsg }}</div>
  </div>
</template>

<style scoped>
.loras-panel {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin: 0;
}

.field-hint {
  margin: 0;
}

.field-hint code {
  background: rgba(255, 255, 255, 0.06);
  padding: 0 3px;
  border-radius: 3px;
  font-size: 10px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
}

.loras-header {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.loras-empty {
  padding: 12px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.02);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
}

.loras-empty code {
  font-family: ui-monospace, Menlo, Consolas, monospace;
  background: rgba(255, 255, 255, 0.06);
  padding: 1px 5px;
  border-radius: 3px;
}

.loras-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.lora-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}

.lora-name {
  font-size: 12px;
  font-family: ui-monospace, Menlo, Consolas, monospace;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.lora-controls {
  display: grid;
  grid-template-columns: 32px 1fr 40px 24px;
  align-items: center;
  gap: 6px;
}

.lora-strength-label {
  font-size: 10px;
  margin: 0;
}

.range-value {
  font-size: 11px;
  color: var(--color-text-secondary);
  text-align: right;
  font-family: ui-monospace, Menlo, Consolas, monospace;
}

.btn-icon {
  width: 24px;
  height: 24px;
  border-radius: 4px;
  border: none;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;
}

.btn-icon:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.btn-icon-danger:hover:not(:disabled) {
  background: rgba(231, 76, 60, 0.25);
  color: #ff8a75;
}

.upload-btn {
  display: block;
  padding: 10px;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  text-align: center;
  font-size: 12px;
  color: var(--color-text-secondary);
  cursor: pointer;
  background: rgba(255, 255, 255, 0.03);
}

.upload-btn:hover { background: rgba(255, 255, 255, 0.06); }
.upload-btn input { display: none; }
.upload-btn.disabled { opacity: 0.5; cursor: not-allowed; }

.attach-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(107, 153, 178, 0.06);
  border: 1px solid rgba(107, 153, 178, 0.2);
}

.attach-row {
  display: grid;
  grid-template-columns: 1fr 60px 60px;
  gap: 6px;
  align-items: center;
}

/* 本元件 row 內欄位用較小字級與 padding，覆蓋全域基準 */
.field-select, .field-input {
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12px;
}

.strength-input {
  text-align: center;
}

/* .field-label 在 global style.css */

/* .field-range 在 global style.css */

.loras-error {
  padding: 6px 10px;
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.4);
  border-radius: 6px;
  color: #ff8a75;
  font-size: 12px;
}
</style>
