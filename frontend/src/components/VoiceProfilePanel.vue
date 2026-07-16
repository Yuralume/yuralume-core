<script setup lang="ts">
/**
 * Per-character TTS override editor.
 *
 * The core app only stores the deployment-facing ``voice_id`` plus the
 * character-level enable/translation knobs. Provider-specific assets and
 * workflows belong to the external TTS service.
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character, VoiceProfile } from '@/types/character'
import { updateCharacter } from '@/utils/api/characters'
import { synthesizeCharacterTTS, TTSDisabledError } from '@/utils/api/tts'
import { listTTSAssets, type TTSAssetCatalog } from '@/utils/api/ttsAssets'
import { UiButton } from '@/components/ui'

const props = defineProps<{
  character: Character
}>()

const emit = defineEmits<{
  updated: [character: Character]
}>()

const { t } = useI18n()

function emptyProfile(): VoiceProfile {
  return {
    enabled: true,
    voice_id: '',
    ref_audio_path: '',
    prompt_text: '',
    prompt_lang: '',
    translate_target_lang: '',
    gpt_weights_path: '',
    sovits_weights_path: '',
  }
}

const form = ref<VoiceProfile>(props.character.voice_profile ?? emptyProfile())
const hasOverride = ref(props.character.voice_profile !== null)
const saving = ref(false)
const errorMsg = ref<string>('')
const successMsg = ref<string>('')
const catalog = ref<TTSAssetCatalog | null>(null)
const catalogLoading = ref(false)

async function loadCatalog() {
  catalogLoading.value = true
  try {
    catalog.value = await listTTSAssets()
  } catch {
    catalog.value = null
  } finally {
    catalogLoading.value = false
  }
}

onMounted(loadCatalog)

const voiceOptions = computed(() => catalog.value?.voice_presets ?? [])
const catalogEnabled = computed(
  () => catalog.value?.enabled === true && voiceOptions.value.length > 0,
)

const selectedVoiceId = computed({
  get() {
    return form.value.voice_id || ''
  },
  set(value: string) {
    form.value.voice_id = value
    const voice = voiceOptions.value.find(v => (v.voice_id || v.id) === value)
    if (voice) {
      form.value.prompt_lang = voice.prompt_lang || ''
    }
    form.value.ref_audio_path = ''
    form.value.prompt_text = ''
    form.value.gpt_weights_path = ''
    form.value.sovits_weights_path = ''
  },
})

watch(
  () => props.character.voice_profile,
  (next) => {
    form.value = next ?? emptyProfile()
    hasOverride.value = next !== null
  },
)

const isEmptyForm = computed(() => {
  const f = form.value
  return (
    f.enabled
    && !f.voice_id
    && !f.translate_target_lang
  )
})

async function handleSave() {
  if (saving.value) return
  saving.value = true
  errorMsg.value = ''
  successMsg.value = ''
  try {
    const payload = isEmptyForm.value ? null : { ...form.value }
    const updated = await updateCharacter(props.character.id, {
      voice_profile: payload,
    })
    emit('updated', updated)
    hasOverride.value = updated.voice_profile !== null
    successMsg.value = payload === null
      ? t('voiceProfilePanel.feedback.cleared')
      : t('voiceProfilePanel.feedback.saved')
  } catch (err) {
    errorMsg.value = extractError(err) ?? t('voiceProfilePanel.errors.saveFailed')
  } finally {
    saving.value = false
  }
}

async function handleClear() {
  form.value = emptyProfile()
  await handleSave()
}

const testing = ref(false)
const testStatus = ref<'idle' | 'busy' | 'ok' | 'error' | 'disabled'>('idle')
const testMsg = ref<string>('')
let testAudio: HTMLAudioElement | null = null

async function handleTest() {
  if (testing.value) return
  if (testAudio) {
    testAudio.pause()
    testAudio = null
    testStatus.value = 'idle'
    return
  }
  testing.value = true
  testStatus.value = 'busy'
  testMsg.value = ''
  try {
    const saved = props.character.voice_profile ?? emptyProfile()
    const dirty = JSON.stringify(saved) !== JSON.stringify(form.value)
    if (dirty) {
      testStatus.value = 'error'
      testMsg.value = t('voiceProfilePanel.test.saveBeforePreview')
      return
    }
    const sample = t('voiceProfilePanel.test.sampleText')
    const res = await synthesizeCharacterTTS(props.character.id, sample)
    testAudio = new Audio(res.audio_url)
    testAudio.onended = () => {
      testStatus.value = 'ok'
      testAudio = null
    }
    testAudio.onerror = () => {
      testStatus.value = 'error'
      testMsg.value = t('voiceProfilePanel.errors.audioPlayFailed')
      testAudio = null
    }
    await testAudio.play()
  } catch (err) {
    if (err instanceof TTSDisabledError) {
      testStatus.value = 'disabled'
      testMsg.value = err.message
    } else {
      testStatus.value = 'error'
      testMsg.value = extractError(err) ?? t('voiceProfilePanel.errors.synthesisFailed')
    }
  } finally {
    testing.value = false
  }
}

function extractError(err: unknown): string | null {
  if (err && typeof err === 'object' && 'response' in err) {
    const resp = (err as { response?: { data?: { detail?: string } } }).response
    if (resp?.data?.detail) return resp.data.detail
  }
  return err instanceof Error ? err.message : null
}

const testButtonLabel = computed(() => {
  if (testAudio) return t('voiceProfilePanel.test.stop')
  switch (testStatus.value) {
    case 'busy': return t('voiceProfilePanel.test.synthesizing')
    case 'ok': return t('voiceProfilePanel.test.replay')
    case 'error': return t('common.actions.retry')
    case 'disabled': return t('voiceProfilePanel.test.disabled')
    default: return t('voiceProfilePanel.test.preview')
  }
})
</script>

<template>
  <div class="voice-profile">
    <p class="field-hint">
      {{ t('voiceProfilePanel.hints.mainPrefix') }}
      <strong>{{ t('voiceProfilePanel.hints.inheritStrongPrefix') }} <code>KOKORO_TTS_*</code> {{ t('voiceProfilePanel.hints.inheritStrongSuffix') }}</strong>
      {{ t('voiceProfilePanel.hints.mainSuffix') }}
    </p>

    <p
      v-if="!catalogLoading && !catalogEnabled"
      class="field-hint field-hint-warn"
    >
      {{ t('voiceProfilePanel.hints.catalogMissingPrefix') }} <code>KOKORO_TTS_BASE_URL</code>
      {{ t('voiceProfilePanel.hints.catalogMissingMiddle') }} <code>.env</code>
      {{ t('voiceProfilePanel.hints.catalogMissingSuffix') }}
    </p>

    <label class="voice-toggle">
      <input v-model="form.enabled" type="checkbox" />
      <span>{{ t('voiceProfilePanel.enabledLabel') }}</span>
    </label>

    <div class="field-group">
      <label class="field-label">
        {{ t('voiceProfilePanel.fields.voicePreset') }}
        <button
          type="button"
          class="rescan-btn"
          :disabled="catalogLoading"
          :title="t('voiceProfilePanel.actions.rescanTitle')"
          @click="loadCatalog"
        >↻</button>
      </label>
      <select
        v-model="selectedVoiceId"
        class="field-select"
        :disabled="catalogLoading || !catalogEnabled"
      >
        <option value="">{{ t('voiceProfilePanel.options.inheritGlobal') }}</option>
        <option
          v-for="voice in voiceOptions"
          :key="voice.id"
          :value="voice.voice_id || voice.id"
          :disabled="!voice.is_complete"
        >
          {{ voice.label }}
        </option>
      </select>
      <p class="field-hint field-hint-subtle">
        {{ t('voiceProfilePanel.hints.preset') }}
      </p>
    </div>

    <div class="field-group">
      <label class="field-label">{{ t('voiceProfilePanel.fields.translateTargetLang') }}</label>
      <select v-model="form.translate_target_lang" class="field-select">
        <option value="">{{ t('voiceProfilePanel.options.inheritGlobal') }}</option>
        <option value="zh">{{ t('voiceProfilePanel.languages.zh') }}</option>
        <option value="ja">{{ t('voiceProfilePanel.languages.ja') }}</option>
        <option value="en">{{ t('voiceProfilePanel.languages.en') }}</option>
        <option value="ko">{{ t('voiceProfilePanel.languages.ko') }}</option>
        <option value="-">{{ t('voiceProfilePanel.options.disableOverride') }}</option>
      </select>
      <p class="field-hint field-hint-subtle">
        {{ t('voiceProfilePanel.hints.dubbingPrefix') }}
        <code>ja</code>
        {{ t('voiceProfilePanel.hints.dubbingSuffix') }}
      </p>
    </div>

    <div class="form-actions voice-actions">
      <UiButton
        size="sm"
        class="test-btn"
        :class="{ 'is-busy': testStatus === 'busy', 'is-error': testStatus === 'error' }"
        :disabled="testing && !testAudio"
        :title="testMsg || ''"
        @click="handleTest"
      >
        {{ testButtonLabel }}
      </UiButton>
      <UiButton
        v-if="hasOverride"
        variant="danger"
        size="sm"
        :disabled="saving"
        @click="handleClear"
      >
        {{ t('voiceProfilePanel.actions.clearToGlobal') }}
      </UiButton>
      <UiButton
        variant="primary"
        size="sm"
        :loading="saving"
        @click="handleSave"
      >
        {{ saving ? t('common.state.saving') : t('common.actions.save') }}
      </UiButton>
    </div>

    <div v-if="errorMsg" class="form-msg form-error">{{ errorMsg }}</div>
    <div v-else-if="successMsg" class="form-msg form-ok">{{ successMsg }}</div>
    <div v-else-if="testMsg" class="form-msg form-warn">{{ testMsg }}</div>
  </div>
</template>

<style scoped>
.voice-profile {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.field-hint {
  font-size: 11px;
  color: var(--color-text-secondary);
  line-height: 1.55;
  margin: 0;
}

.field-hint-subtle {
  opacity: 0.75;
}

.field-hint-warn {
  background: rgba(243, 156, 18, 0.08);
  border: 1px solid rgba(243, 156, 18, 0.3);
  border-radius: 4px;
  padding: 6px 8px;
  color: #f6b94a;
}

.rescan-btn {
  margin-left: 6px;
  background: transparent;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  border-radius: 50%;
  width: 18px;
  height: 18px;
  font-size: 10px;
  line-height: 1;
  padding: 0;
  cursor: pointer;
}

.rescan-btn:hover:not(:disabled) {
  color: var(--color-text);
  background: rgba(255, 255, 255, 0.06);
}

.rescan-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.voice-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--color-text);
  cursor: pointer;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  background: rgba(255, 255, 255, 0.03);
  border-radius: 6px;
}

.field-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.voice-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
  flex-wrap: wrap;
}

.test-btn.is-busy {
  color: var(--color-text-secondary);
  opacity: 0.7;
}

.test-btn.is-error {
  color: #ff8a75;
  border-color: rgba(255, 138, 117, 0.45);
}

.form-msg {
  font-size: 11px;
  padding: 6px 8px;
  border-radius: 4px;
}

.form-error {
  background: rgba(231, 76, 60, 0.12);
  border: 1px solid rgba(231, 76, 60, 0.35);
  color: #ff8a75;
}

.form-ok {
  background: rgba(46, 204, 113, 0.1);
  border: 1px solid rgba(46, 204, 113, 0.35);
  color: #69d292;
}

.form-warn {
  background: rgba(243, 156, 18, 0.12);
  border: 1px solid rgba(243, 156, 18, 0.35);
  color: #f6b94a;
}
</style>
