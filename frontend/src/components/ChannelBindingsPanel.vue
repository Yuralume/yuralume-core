<script setup lang="ts">
import axios from 'axios'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type {
  ChannelBinding,
  DeliveryMode,
  MessagingAccount,
  MessagingSettingsResponse,
  MessagingPlatform,
} from '@/types/messaging'
import {
  createAccount,
  createBinding,
  deleteAccount,
  deleteBinding,
  getMessagingSettings,
  getWebhookStatus,
  listAccounts,
  listBindings,
  registerWebhook,
  setBindingAcceptsProactive,
  setBindingEnabled,
  updateAccount,
  whatsappQrSvgUrl,
} from '@/utils/api/messaging'
import { UiButton } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import ChannelSetupGuide from './ChannelSetupGuide.vue'
import ChannelAccountNextStep from './ChannelAccountNextStep.vue'
import ChannelProactiveAttemptLog from './ChannelProactiveAttemptLog.vue'

const props = withDefaults(defineProps<{
  characterId: string | null
  initialPlatform?: MessagingPlatform | null
  openCreateSignal?: number
}>(), {
  initialPlatform: null,
  openCreateSignal: 0,
})

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const PLATFORM_LABELS: Record<MessagingPlatform, string> = {
  telegram: 'Telegram',
  line: 'LINE',
  discord: 'Discord',
  whatsapp: 'WhatsApp',
}

const accounts = ref<MessagingAccount[]>([])
const bindingsByAccount = reactive<Record<string, ChannelBinding[]>>({})
const expandedAccountId = ref<string | null>(null)
const loading = ref(false)
const errorMessage = ref<string | null>(null)
const busyId = ref<string | null>(null)

type WebhookFeedback = { ok: boolean; message: string }
const webhookFeedback = reactive<Record<string, WebhookFeedback>>({})

const effectivePublicBaseUrl = ref('')
const publicBaseUrlSource = ref<MessagingSettingsResponse['source']>('empty')
const siteTelegramDeliveryMode = ref<DeliveryMode>('polling')
const settingsLoading = ref(false)
const settingsError = ref<string | null>(null)
const setupGuidePlatform = ref<MessagingPlatform>('telegram')
const lastCreatedAccountId = ref<string | null>(null)
const whatsappQrVersion = reactive<Record<string, number>>({})
const whatsappQrErrors = reactive<Record<string, boolean>>({})

const publicBaseUrlSourceLabel = computed(() => {
  if (publicBaseUrlSource.value === 'preference') {
    return t('channelBindingsPanel.publicBaseUrl.source.preference')
  }
  if (publicBaseUrlSource.value === 'env') {
    return t('channelBindingsPanel.publicBaseUrl.source.env')
  }
  return t('channelBindingsPanel.publicBaseUrl.source.empty')
})

const canRegisterWebhook = computed(() => effectivePublicBaseUrl.value.length > 0)

onMounted(() => {
  void loadMessagingSettings()
  if (props.openCreateSignal > 0 && props.initialPlatform) {
    openCreateAccountForm(props.initialPlatform)
  }
})

function applyMessagingSettings(settings: MessagingSettingsResponse) {
  effectivePublicBaseUrl.value = settings.effective_public_base_url
  publicBaseUrlSource.value = settings.source
  siteTelegramDeliveryMode.value = settings.telegram_delivery_mode
}

async function loadMessagingSettings() {
  settingsLoading.value = true
  settingsError.value = null
  try {
    applyMessagingSettings(await getMessagingSettings())
  } catch (err) {
    settingsError.value = readError(err, t('channelBindingsPanel.errors.loadSettingsFailed'))
  } finally {
    settingsLoading.value = false
  }
}

// Create-account form state (a single inline form under the accounts list)
const showCreate = ref(false)
const createPlatform = ref<MessagingPlatform>('telegram')
const createDisplayName = ref('')
const createBotToken = ref('')
const createWebhookSecret = ref('')
const createChannelSecret = ref('')
const createChannelAccessToken = ref('')
const createAllowlist = ref('')
const creating = ref(false)
const createError = ref<string | null>(null)

const canSubmitCreate = computed(() => {
  if (creating.value) return false
  if (createPlatform.value === 'telegram' || createPlatform.value === 'discord') {
    return createBotToken.value.trim().length > 0
  }
  if (createPlatform.value === 'whatsapp') {
    return true
  }
  return (
    createChannelSecret.value.trim().length > 0
    && createChannelAccessToken.value.trim().length > 0
  )
})

watch(() => props.characterId, async () => {
  await refresh()
  lastCreatedAccountId.value = null
}, { immediate: true })

watch(() => props.openCreateSignal, (next, prev) => {
  if (next === prev || next <= 0) return
  openCreateAccountForm(props.initialPlatform ?? 'telegram')
})

watch(createPlatform, (platform) => {
  setupGuidePlatform.value = platform
})

async function refresh() {
  accounts.value = []
  for (const key of Object.keys(bindingsByAccount)) delete bindingsByAccount[key]
  expandedAccountId.value = null
  errorMessage.value = null
  if (!props.characterId) return

  loading.value = true
  try {
    accounts.value = await listAccounts(props.characterId)
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.loadAccountsFailed'))
  } finally {
    loading.value = false
  }
}

async function toggleExpand(account: MessagingAccount) {
  if (expandedAccountId.value === account.id) {
    expandedAccountId.value = null
    return
  }
  expandedAccountId.value = account.id
  if (!(account.id in bindingsByAccount)) {
    await loadBindings(account.id)
  }
}

async function loadBindings(accountId: string) {
  try {
    bindingsByAccount[accountId] = await listBindings(accountId)
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.loadBindingsFailed'))
  }
}

async function refreshBindings(accountId: string) {
  busyId.value = accountId
  errorMessage.value = null
  try {
    await loadBindings(accountId)
  } finally {
    busyId.value = null
  }
}

function resetCreateForm() {
  createDisplayName.value = ''
  createBotToken.value = ''
  createWebhookSecret.value = ''
  createChannelSecret.value = ''
  createChannelAccessToken.value = ''
  createAllowlist.value = ''
  createError.value = null
}

function openCreateAccountForm(platform = setupGuidePlatform.value) {
  setupGuidePlatform.value = platform
  createPlatform.value = platform
  resetCreateForm()
  showCreate.value = true
}

function selectSetupGuidePlatform(platform: MessagingPlatform) {
  setupGuidePlatform.value = platform
  if (showCreate.value) {
    createPlatform.value = platform
  }
}

async function handleCreate() {
  if (!props.characterId || !canSubmitCreate.value) return
  creating.value = true
  createError.value = null
  try {
    const credentials: Record<string, string> = {}
    if (createPlatform.value === 'telegram' || createPlatform.value === 'discord') {
      credentials.bot_token = createBotToken.value.trim()
      if (createPlatform.value === 'telegram' && createWebhookSecret.value.trim()) {
        credentials.webhook_secret = createWebhookSecret.value.trim()
      }
    } else if (createPlatform.value === 'whatsapp') {
      // WhatsApp deployment details are owned by the containerized gateway.
      // The backend fills sidecar_url/session_id from product defaults.
    } else {
      credentials.channel_secret = createChannelSecret.value.trim()
      credentials.channel_access_token = createChannelAccessToken.value.trim()
    }
    const allowlist = splitLines(createAllowlist.value)
    const account = await createAccount({
      character_id: props.characterId,
      platform: createPlatform.value,
      display_name: createDisplayName.value.trim(),
      credentials,
      allowed_sender_refs: allowlist,
    })
    accounts.value = [account, ...accounts.value]
    expandedAccountId.value = account.id
    lastCreatedAccountId.value = account.id
    setupGuidePlatform.value = account.platform
    resetCreateForm()
    showCreate.value = false
  } catch (err) {
    createError.value = readError(err, t('channelBindingsPanel.errors.createAccountFailed'))
  } finally {
    creating.value = false
  }
}

async function handleToggleEnabled(account: MessagingAccount) {
  busyId.value = account.id
  errorMessage.value = null
  try {
    const updated = await updateAccount(account.id, { enabled: !account.enabled })
    accounts.value = accounts.value.map(a => a.id === updated.id ? updated : a)
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.updateAccountStatusFailed'))
  } finally {
    busyId.value = null
  }
}

async function handleSaveAllowlist(account: MessagingAccount, raw: string) {
  busyId.value = account.id
  errorMessage.value = null
  try {
    const updated = await updateAccount(account.id, {
      allowed_sender_refs: splitLines(raw),
    })
    accounts.value = accounts.value.map(a => a.id === updated.id ? updated : a)
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.updateAllowlistFailed'))
  } finally {
    busyId.value = null
  }
}

async function handleDeleteAccount(account: MessagingAccount) {
  const ok = await confirmDialog({
    content: t('channelBindingsPanel.confirm.deleteAccount', {
      name: account.display_name || PLATFORM_LABELS[account.platform],
    }),
    okText: t('common.actions.delete'),
    danger: true,
  })
  if (!ok) return
  busyId.value = account.id
  errorMessage.value = null
  try {
    await deleteAccount(account.id)
    accounts.value = accounts.value.filter(a => a.id !== account.id)
    delete bindingsByAccount[account.id]
    if (expandedAccountId.value === account.id) expandedAccountId.value = null
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.deleteAccountFailed'))
  } finally {
    busyId.value = null
  }
}

async function handleToggleBinding(accountId: string, binding: ChannelBinding) {
  busyId.value = binding.id
  errorMessage.value = null
  try {
    const updated = await setBindingEnabled(binding.id, !binding.enabled)
    bindingsByAccount[accountId] = (bindingsByAccount[accountId] ?? []).map(
      b => b.id === updated.id ? updated : b,
    )
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.toggleBindingFailed'))
  } finally {
    busyId.value = null
  }
}

async function handleToggleAcceptsProactive(
  accountId: string, binding: ChannelBinding,
) {
  busyId.value = binding.id
  errorMessage.value = null
  try {
    const updated = await setBindingAcceptsProactive(
      binding.id, !binding.accepts_proactive,
    )
    bindingsByAccount[accountId] = (bindingsByAccount[accountId] ?? []).map(
      b => b.id === updated.id ? updated : b,
    )
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.toggleProactiveFailed'))
  } finally {
    busyId.value = null
  }
}

async function handleDeleteBinding(accountId: string, binding: ChannelBinding) {
  const ok = await confirmDialog({
    content: t('channelBindingsPanel.confirm.deleteBinding', {
      chat: binding.chat_ref,
    }),
    okText: t('common.actions.delete'),
    danger: true,
  })
  if (!ok) return
  busyId.value = binding.id
  errorMessage.value = null
  try {
    await deleteBinding(binding.id)
    bindingsByAccount[accountId] = (bindingsByAccount[accountId] ?? []).filter(
      b => b.id !== binding.id,
    )
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.deleteBindingFailed'))
  } finally {
    busyId.value = null
  }
}

// Binding manual create — optional flow for pre-registering a chat
const manualBindingInput = reactive<Record<string, string>>({})

async function handleCreateBinding(accountId: string) {
  const chatRef = (manualBindingInput[accountId] ?? '').trim()
  if (!chatRef) return
  busyId.value = accountId
  errorMessage.value = null
  try {
    const binding = await createBinding({ account_id: accountId, chat_ref: chatRef })
    bindingsByAccount[accountId] = [binding, ...(bindingsByAccount[accountId] ?? [])]
    manualBindingInput[accountId] = ''
  } catch (err) {
    errorMessage.value = readError(err, t('channelBindingsPanel.errors.createBindingFailed'))
  } finally {
    busyId.value = null
  }
}

function webhookUrlFor(account: MessagingAccount): string {
  const base = effectivePublicBaseUrl.value
    || (typeof window !== 'undefined' ? window.location.origin : '')
  return `${base}/api/v1/messaging/${account.platform}/webhook/${account.webhook_slug}`
}

function whatsappQrImageUrl(account: MessagingAccount): string {
  const version = whatsappQrVersion[account.id] ?? 0
  const base = whatsappQrSvgUrl(account.id)
  const separator = base.includes('?') ? '&' : '?'
  return `${base}${separator}v=${version}`
}

function refreshWhatsAppQr(account: MessagingAccount) {
  whatsappQrErrors[account.id] = false
  whatsappQrVersion[account.id] = (whatsappQrVersion[account.id] ?? 0) + 1
}

function handleWhatsAppQrLoaded(account: MessagingAccount) {
  whatsappQrErrors[account.id] = false
}

function handleWhatsAppQrError(account: MessagingAccount) {
  whatsappQrErrors[account.id] = true
}

function usesWebhookDelivery(account: MessagingAccount): boolean {
  return account.platform === 'line'
    || (account.platform === 'telegram' && siteTelegramDeliveryMode.value === 'webhook')
}

function allowlistPlaceholder(account: MessagingAccount): string {
  if (account.platform === 'telegram') {
    return t('channelBindingsPanel.allowlist.telegramPlaceholder')
  }
  if (account.platform === 'discord') {
    return t('channelBindingsPanel.allowlist.discordPlaceholder')
  }
  if (account.platform === 'whatsapp') {
    return t('channelBindingsPanel.allowlist.whatsappPlaceholder')
  }
  return t('channelBindingsPanel.allowlist.linePlaceholder')
}

function bindingPlaceholder(account: MessagingAccount): string {
  if (account.platform === 'telegram') {
    return t('channelBindingsPanel.bindings.telegramPlaceholder')
  }
  if (account.platform === 'discord') {
    return t('channelBindingsPanel.bindings.discordPlaceholder')
  }
  if (account.platform === 'whatsapp') {
    return t('channelBindingsPanel.bindings.whatsappPlaceholder')
  }
  return t('channelBindingsPanel.bindings.linePlaceholder')
}

function bindingCount(accountId: string): number {
  return bindingsByAccount[accountId]?.length ?? 0
}

async function handleRegisterWebhook(account: MessagingAccount) {
  if (!canRegisterWebhook.value) {
    webhookFeedback[account.id] = {
      ok: false,
      message: t('channelBindingsPanel.webhook.needPublicBaseUrl'),
    }
    return
  }
  busyId.value = account.id
  errorMessage.value = null
  try {
    const result = await registerWebhook(account.id)
    webhookFeedback[account.id] = {
      ok: result.ok,
      message: result.ok
        ? t('channelBindingsPanel.webhook.registeredTo', { url: result.webhook_url })
        : t('channelBindingsPanel.webhook.platformFailed', {
          reason: result.message ?? t('common.errors.unknown'),
        }),
    }
  } catch (err) {
    webhookFeedback[account.id] = {
      ok: false,
      message: readError(err, t('channelBindingsPanel.errors.registerWebhookFailed')),
    }
  } finally {
    busyId.value = null
  }
}

async function handleCheckWebhookStatus(account: MessagingAccount) {
  busyId.value = account.id
  errorMessage.value = null
  try {
    const result = await getWebhookStatus(account.id)
    webhookFeedback[account.id] = {
      ok: result.ok,
      message: formatStatus(account, result.info, result.message, result.ok),
    }
  } catch (err) {
    webhookFeedback[account.id] = {
      ok: false,
      message: readError(err, t('channelBindingsPanel.errors.checkStatusFailed')),
    }
  } finally {
    busyId.value = null
  }
}

function formatStatus(
  account: MessagingAccount,
  info: Record<string, unknown> | null | undefined,
  message: string | null | undefined,
  ok: boolean,
): string {
  if (!ok) {
    return t('channelBindingsPanel.webhook.statusFailed', {
      reason: message ?? t('common.errors.unknown'),
    })
  }
  if (!info) return t('channelBindingsPanel.webhook.statusOkNoDetails')
  if (account.platform === 'telegram') {
    const url = info.url ?? t('common.fallback.notSet')
    const pending = info.pending_update_count ?? 0
    const lastError = info.last_error_message
    const parts = [t('channelBindingsPanel.webhook.currentUrl', { url }), `pending: ${pending}`]
    if (lastError) parts.push(`last_error: ${lastError}`)
    return parts.join('  |  ')
  }
  // LINE
  const endpoint = info.endpoint ?? t('common.fallback.notSet')
  const active = info.active ?? false
  return `endpoint: ${endpoint}  |  active: ${active ? 'yes' : 'no'}`
}

async function copyToClipboard(text: string, feedbackKey: string) {
  try {
    await navigator.clipboard.writeText(text)
    copyFeedback.value = feedbackKey
    setTimeout(() => {
      if (copyFeedback.value === feedbackKey) copyFeedback.value = null
    }, 1500)
  } catch {
    errorMessage.value = t('channelBindingsPanel.errors.copyFailed')
  }
}

const copyFeedback = ref<string | null>(null)

function platformHint(platform: MessagingPlatform): string {
  return t(`channelBindingsPanel.platformHints.${platform}`)
}

function splitLines(raw: string): string[] {
  return raw
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(line => line.length > 0)
}

function readError(err: unknown, fallback: string): string {
  if (axios.isAxiosError(err)) {
    const detail = err.response?.data?.detail
    if (typeof detail === 'string') return detail
  }
  return fallback
}
</script>

<template>
  <div class="channels-panel">
    <div v-if="!characterId" class="channels-empty">
      {{ t('channelBindingsPanel.empty.selectCharacter') }}
    </div>

    <template v-else>
      <div class="channels-header">
        <h3 class="section-title">{{ t('channelBindingsPanel.accounts.title') }}</h3>
        <p class="channels-hint">
          {{ t('channelBindingsPanel.accounts.hint') }}
        </p>
      </div>

      <div v-if="errorMessage" class="channels-error">{{ errorMessage }}</div>
      <div v-if="settingsError" class="channels-error">{{ settingsError }}</div>

      <div
        v-if="!loading && (accounts.length === 0 || showCreate)"
        class="setup-guide-block"
      >
        <div class="guide-platform-switch" role="tablist">
          <button
            type="button"
            :class="['guide-platform-button', { active: setupGuidePlatform === 'telegram' }]"
            @click="selectSetupGuidePlatform('telegram')"
          >
            Telegram
          </button>
          <button
            type="button"
            :class="['guide-platform-button', { active: setupGuidePlatform === 'line' }]"
            @click="selectSetupGuidePlatform('line')"
          >
            LINE
          </button>
          <button
            type="button"
            :class="['guide-platform-button', { active: setupGuidePlatform === 'discord' }]"
            @click="selectSetupGuidePlatform('discord')"
          >
            Discord
          </button>
          <button
            type="button"
            :class="['guide-platform-button', { active: setupGuidePlatform === 'whatsapp' }]"
            @click="selectSetupGuidePlatform('whatsapp')"
          >
            WhatsApp
          </button>
        </div>
        <ChannelSetupGuide
          :platform="setupGuidePlatform"
          :telegram-delivery-mode="siteTelegramDeliveryMode"
          :effective-public-base-url="effectivePublicBaseUrl"
          compact
        />
      </div>

      <div v-if="loading" class="channels-empty">{{ t('common.state.loading') }}</div>

      <div v-else>
        <div v-if="accounts.length === 0" class="channels-empty">
          {{ t('channelBindingsPanel.empty.noAccounts') }}
        </div>

        <div v-else class="account-list">
          <div
            v-for="account in accounts"
            :key="account.id"
            :class="[
              'account-card',
              { disabled: !account.enabled, 'just-created': lastCreatedAccountId === account.id },
            ]"
          >
            <div class="account-summary" @click="toggleExpand(account)">
              <div class="account-title">
                <span :class="['platform-badge', `badge-${account.platform}`]">
                  {{ PLATFORM_LABELS[account.platform] }}
                </span>
                <span class="account-name">
                  {{ account.display_name || t('channelBindingsPanel.fallback.unnamed') }}
                </span>
              </div>
              <div class="account-meta">
                <span v-if="account.enabled" class="state-tag enabled">
                  {{ t('channelBindingsPanel.state.enabled') }}
                </span>
                <span v-else class="state-tag disabled">
                  {{ t('channelBindingsPanel.state.disabled') }}
                </span>
                <span class="chevron">
                  {{ expandedAccountId === account.id ? '▾' : '▸' }}
                </span>
              </div>
            </div>

            <div v-if="expandedAccountId === account.id" class="account-detail">
              <ChannelAccountNextStep
                :account="account"
                :telegram-delivery-mode="siteTelegramDeliveryMode"
                :effective-public-base-url="effectivePublicBaseUrl"
                :binding-count="bindingCount(account.id)"
              />

              <div v-if="usesWebhookDelivery(account)" class="detail-section">
                <label class="field-label">{{ t('channelBindingsPanel.webhook.urlLabel') }}</label>
                <div class="copy-row">
                  <code class="copy-text">{{ webhookUrlFor(account) }}</code>
                  <UiButton
                    size="sm"
                    @click="copyToClipboard(webhookUrlFor(account), `url-${account.id}`)"
                  >
                    {{
                      copyFeedback === `url-${account.id}`
                        ? t('channelBindingsPanel.actions.copied')
                        : t('channelBindingsPanel.actions.copy')
                    }}
                  </UiButton>
                </div>
                <div class="webhook-actions">
                  <UiButton
                    variant="primary"
                    size="sm"
                    :disabled="busyId === account.id || !canRegisterWebhook"
                    :title="!canRegisterWebhook ? t('channelBindingsPanel.webhook.needPublicBaseUrl') : ''"
                    @click="handleRegisterWebhook(account)"
                  >{{ t('channelBindingsPanel.actions.registerWebhook') }}</UiButton>
                  <UiButton
                    size="sm"
                    :disabled="busyId === account.id"
                    @click="handleCheckWebhookStatus(account)"
                  >{{ t('channelBindingsPanel.actions.checkStatus') }}</UiButton>
                </div>
                <p class="detail-hint">
                  {{ t('channelBindingsPanel.publicBaseUrl.effective', {
                    url: effectivePublicBaseUrl || t('common.fallback.notSet'),
                    source: publicBaseUrlSourceLabel,
                  }) }}
                </p>
                <div
                  v-if="webhookFeedback[account.id]"
                  :class="['webhook-feedback', webhookFeedback[account.id].ok ? 'ok' : 'fail']"
                >
                  {{ webhookFeedback[account.id].message }}
                </div>
                <p v-if="!canRegisterWebhook" class="detail-hint">
                  {{ t('channelBindingsPanel.publicBaseUrl.missingHint') }}
                </p>
              </div>

              <div v-if="account.platform === 'whatsapp'" class="detail-section">
                <label class="field-label">{{ t('channelBindingsPanel.whatsappQr.label') }}</label>
                <div :class="['whatsapp-qr-frame', { unavailable: whatsappQrErrors[account.id] }]">
                  <img
                    v-show="!whatsappQrErrors[account.id]"
                    :src="whatsappQrImageUrl(account)"
                    :alt="t('channelBindingsPanel.whatsappQr.alt')"
                    @load="handleWhatsAppQrLoaded(account)"
                    @error="handleWhatsAppQrError(account)"
                  />
                  <span v-if="whatsappQrErrors[account.id]" class="whatsapp-qr-frame__empty">
                    {{ t('channelBindingsPanel.whatsappQr.unavailable') }}
                  </span>
                </div>
                <div class="webhook-actions">
                  <UiButton
                    size="sm"
                    :disabled="busyId === account.id"
                    @click="refreshWhatsAppQr(account)"
                  >{{ t('channelBindingsPanel.actions.refreshQr') }}</UiButton>
                </div>
                <p class="detail-hint">
                  {{ t('channelBindingsPanel.whatsappQr.hint') }}
                </p>
              </div>

              <div class="detail-section">
                <label class="field-label">{{ t('channelBindingsPanel.allowlist.label') }}</label>
                <textarea
                  :value="account.allowed_sender_refs.join('\n')"
                  class="field-textarea"
                  rows="3"
                  :placeholder="allowlistPlaceholder(account)"
                  :disabled="busyId === account.id"
                  @change="handleSaveAllowlist(account, ($event.target as HTMLTextAreaElement).value)"
                />
                <p class="detail-hint">
                  {{ t('channelBindingsPanel.allowlist.hint') }}
                </p>
              </div>

              <div class="detail-section">
                <div class="bindings-header">
                  <label class="field-label">{{ t('channelBindingsPanel.bindings.title') }}</label>
                  <small class="detail-hint">
                    {{ t('channelBindingsPanel.bindings.hint') }}
                  </small>
                  <UiButton
                    size="sm"
                    :disabled="busyId === account.id"
                    @click="refreshBindings(account.id)"
                  >{{ t('channelBindingsPanel.actions.refreshBindings') }}</UiButton>
                </div>

                <div class="binding-create">
                  <input
                    v-model="manualBindingInput[account.id]"
                    class="field-input"
                    :placeholder="bindingPlaceholder(account)"
                    :disabled="busyId === account.id"
                  />
                  <UiButton
                    variant="primary"
                    size="sm"
                    :disabled="busyId === account.id || !(manualBindingInput[account.id]?.trim())"
                    @click="handleCreateBinding(account.id)"
                  >{{ t('channelBindingsPanel.actions.addBinding') }}</UiButton>
                </div>

                <div
                  v-if="(bindingsByAccount[account.id]?.length ?? 0) === 0"
                  class="binding-empty"
                >
                  {{ t('channelBindingsPanel.empty.noBindings') }}
                </div>
                <div v-else class="binding-list">
                  <div
                    v-for="binding in bindingsByAccount[account.id]"
                    :key="binding.id"
                    :class="['binding-row', { disabled: !binding.enabled }]"
                  >
                    <code class="binding-chat">{{ binding.chat_ref }}</code>
                    <span v-if="binding.enabled" class="state-tag enabled">
                      {{ t('channelBindingsPanel.state.enabled') }}
                    </span>
                    <span v-else class="state-tag disabled">
                      {{ t('channelBindingsPanel.state.disabled') }}
                    </span>
                    <span
                      v-if="binding.accepts_proactive"
                      class="state-tag proactive"
                      :title="t('channelBindingsPanel.bindings.acceptsProactiveTitle')"
                    >🔔 {{ t('channelBindingsPanel.bindings.proactiveBadge') }}</span>
                    <div class="binding-actions">
                      <button
                        class="icon-btn"
                        :disabled="busyId === binding.id"
                        :title="binding.accepts_proactive
                          ? t('channelBindingsPanel.actions.disableProactive')
                          : t('channelBindingsPanel.actions.enableProactive')"
                        @click="handleToggleAcceptsProactive(account.id, binding)"
                      >{{ binding.accepts_proactive ? '🔔' : '🔕' }}</button>
                      <button
                        class="icon-btn"
                        :disabled="busyId === binding.id"
                        :title="binding.enabled
                          ? t('channelBindingsPanel.actions.disable')
                          : t('channelBindingsPanel.actions.enable')"
                        @click="handleToggleBinding(account.id, binding)"
                      >{{ binding.enabled ? '⏸' : '▶' }}</button>
                      <button
                        class="icon-btn delete-btn"
                        :disabled="busyId === binding.id"
                        :title="t('channelBindingsPanel.actions.removeBinding')"
                        @click="handleDeleteBinding(account.id, binding)"
                      >×</button>
                    </div>
                  </div>
                </div>
              </div>

              <div class="detail-actions">
                <UiButton
                  size="sm"
                  :disabled="busyId === account.id"
                  @click="handleToggleEnabled(account)"
                >
                  {{
                    account.enabled
                      ? t('channelBindingsPanel.actions.disableAccount')
                      : t('channelBindingsPanel.actions.enableAccount')
                  }}
                </UiButton>
                <UiButton
                  variant="danger"
                  size="sm"
                  :disabled="busyId === account.id"
                  @click="handleDeleteAccount(account)"
                >{{ t('channelBindingsPanel.actions.deleteAccount') }}</UiButton>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div class="create-toggle-row">
        <UiButton
          variant="primary"
          :disabled="showCreate"
          @click="openCreateAccountForm()"
        >{{ t('channelBindingsPanel.actions.addAccount') }}</UiButton>
      </div>

      <ChannelProactiveAttemptLog :character-id="characterId" />

      <div v-if="showCreate" class="create-form">
        <h4 class="section-title">{{ t('channelBindingsPanel.create.title') }}</h4>
        <p class="detail-hint">{{ platformHint(createPlatform) }}</p>

        <label class="field-label">{{ t('channelBindingsPanel.create.platform') }}</label>
        <select v-model="createPlatform" class="field-select">
          <option value="telegram">Telegram</option>
          <option value="line">LINE</option>
          <option value="discord">Discord</option>
          <option value="whatsapp">WhatsApp</option>
        </select>

        <label class="field-label">{{ t('channelBindingsPanel.create.displayName') }}</label>
        <input
          v-model="createDisplayName"
          class="field-input"
          :placeholder="t('channelBindingsPanel.create.displayNamePlaceholder')"
        />

        <template v-if="createPlatform === 'telegram' || createPlatform === 'discord'">
          <label class="field-label">{{ t('channelBindingsPanel.create.botTokenLabel') }}</label>
          <input
            v-model="createBotToken"
            class="field-input"
            type="password"
            placeholder="123456:ABCDEF..."
          />
          <template v-if="createPlatform === 'telegram'">
            <label class="field-label">{{ t('channelBindingsPanel.create.webhookSecret') }}</label>
            <input
              v-model="createWebhookSecret"
              class="field-input"
              type="password"
              :placeholder="t('channelBindingsPanel.create.webhookSecretPlaceholder')"
            />
          </template>
        </template>

        <template v-else-if="createPlatform === 'line'">
          <label class="field-label">{{ t('channelBindingsPanel.create.channelSecretLabel') }}</label>
          <input
            v-model="createChannelSecret"
            class="field-input"
            type="password"
          />
          <label class="field-label">{{ t('channelBindingsPanel.create.channelAccessTokenLabel') }}</label>
          <input
            v-model="createChannelAccessToken"
            class="field-input"
            type="password"
          />
        </template>

        <template v-else-if="createPlatform === 'whatsapp'">
          <p class="detail-hint">
            {{ t('channelBindingsPanel.create.whatsappManagedByDeployment') }}
          </p>
        </template>

        <label class="field-label">{{ t('channelBindingsPanel.create.allowlist') }}</label>
        <textarea
          v-model="createAllowlist"
          class="field-textarea"
          rows="3"
          :placeholder="t('channelBindingsPanel.create.allowlistPlaceholder')"
        />

        <div v-if="createError" class="channels-error">{{ createError }}</div>

        <div class="create-actions">
          <UiButton @click="showCreate = false">{{ t('common.actions.cancel') }}</UiButton>
          <UiButton
            variant="primary"
            :loading="creating"
            :disabled="!canSubmitCreate"
            @click="handleCreate"
          >{{ creating ? t('channelBindingsPanel.actions.creating') : t('channelBindingsPanel.actions.create') }}</UiButton>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.channels-panel {
  padding: 12px 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.channels-header .section-title,
.create-form .section-title {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 600;
}

.channels-hint,
.channels-empty,
.detail-hint,
.binding-empty {
  font-size: 12px;
  color: var(--text-secondary, #888);
  line-height: 1.6;
}

.detail-hint code {
  background: rgba(128, 128, 128, 0.12);
  padding: 0 4px;
  border-radius: 3px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
}

.channels-error {
  background: rgba(255, 77, 79, 0.12);
  border: 1px solid rgba(255, 77, 79, 0.5);
  color: #ff4d4f;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12px;
}

/* 共用欄位樣式在 global style.css */

.account-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.account-card {
  border: 1px solid var(--border-color, #2a2a2a);
  border-radius: 6px;
  background: var(--card-bg, rgba(255, 255, 255, 0.02));
  overflow: hidden;
}

.account-card.disabled {
  opacity: 0.65;
}

.account-card.just-created {
  border-color: rgba(64, 156, 255, 0.45);
}

.account-summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  cursor: pointer;
}

.account-summary:hover {
  background: rgba(128, 128, 128, 0.06);
}

.account-title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.account-name {
  font-size: 13px;
  font-weight: 500;
}

.account-meta {
  display: flex;
  align-items: center;
  gap: 8px;
}

.chevron {
  font-size: 11px;
  color: var(--text-secondary, #888);
}

.platform-badge {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 600;
}

.badge-telegram {
  background: rgba(34, 158, 217, 0.15);
  color: #229ed9;
}

.badge-line {
  background: rgba(6, 199, 85, 0.15);
  color: #06c755;
}

.badge-whatsapp {
  background: rgba(37, 211, 102, 0.15);
  color: #25d366;
}

.state-tag {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 3px;
}

.state-tag.enabled {
  background: rgba(82, 196, 26, 0.15);
  color: #52c41a;
}

.state-tag.disabled {
  background: rgba(128, 128, 128, 0.15);
  color: #999;
}

.account-detail {
  border-top: 1px solid var(--border-color, #2a2a2a);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-section {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.copy-row {
  display: flex;
  gap: 6px;
  align-items: stretch;
}

.copy-text {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid var(--border-color, #2a2a2a);
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.08);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  overflow-x: auto;
  white-space: nowrap;
}

.bindings-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  margin-bottom: 4px;
}

.binding-create {
  display: flex;
  gap: 6px;
  margin-bottom: 6px;
}

.binding-create .field-input {
  flex: 1;
}

.binding-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.binding-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: 1px solid var(--border-color, #2a2a2a);
  border-radius: 4px;
  background: rgba(128, 128, 128, 0.04);
}

.binding-row.disabled { opacity: 0.6; }

.binding-chat {
  flex: 1;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  word-break: break-all;
}

.binding-actions {
  display: flex;
  gap: 4px;
}

.icon-btn {
  background: transparent;
  border: 1px solid var(--border-color, #333);
  color: inherit;
  border-radius: 3px;
  width: 24px;
  height: 24px;
  cursor: pointer;
  font-size: 11px;
  padding: 0;
}

.icon-btn:hover:not(:disabled) {
  background: rgba(128, 128, 128, 0.12);
}

.icon-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.icon-btn.delete-btn:hover:not(:disabled) {
  color: #ff4d4f;
  border-color: #ff4d4f;
}

.detail-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}

.create-toggle-row {
  margin-top: 4px;
}

.create-form {
  border: 1px solid var(--border-color, #2a2a2a);
  border-radius: 6px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.create-actions {
  display: flex;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 8px;
}

.webhook-actions {
  display: flex;
  gap: 6px;
  margin-top: 6px;
}

.webhook-feedback {
  margin-top: 6px;
  padding: 6px 8px;
  border-radius: 4px;
  font-size: 12px;
  line-height: 1.55;
  word-break: break-all;
}

.webhook-feedback.ok {
  background: rgba(82, 196, 26, 0.12);
  border: 1px solid rgba(82, 196, 26, 0.5);
  color: #52c41a;
}

.webhook-feedback.fail {
  background: rgba(255, 77, 79, 0.12);
  border: 1px solid rgba(255, 77, 79, 0.5);
  color: #ff4d4f;
}

.whatsapp-qr-frame {
  width: 168px;
  height: 168px;
  display: grid;
  place-items: center;
  border: 1px solid var(--border-color, #2a2a2a);
  border-radius: 6px;
  background: #fff;
  padding: 8px;
}

.whatsapp-qr-frame img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.whatsapp-qr-frame.unavailable {
  background: rgba(255, 255, 255, 0.04);
}

.whatsapp-qr-frame__empty {
  color: var(--color-text-secondary);
  font-size: 12px;
  text-align: center;
  line-height: 1.45;
}

.state-tag.proactive {
  background: rgba(251, 191, 36, 0.15);
  color: #fbbf24;
}

.setup-guide-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.guide-platform-switch {
  display: inline-flex;
  align-self: flex-start;
  padding: 2px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
}

.guide-platform-button {
  border: 0;
  border-radius: 4px;
  background: transparent;
  color: var(--color-text-secondary);
  cursor: pointer;
  font: inherit;
  font-size: 12px;
  padding: 4px 8px;
}

.guide-platform-button.active {
  background: rgba(255, 255, 255, 0.1);
  color: var(--color-text);
}
</style>
