<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import type { ComponentPublicInstance } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useLocale } from '@/composables/useLocale'
import { formatDateTime } from '@/i18n/formatters'
import type { OperatorProfile } from '@/types/operator'
import {
  getOperatorProfile,
  updateOperatorProfile,
} from '@/utils/api/operatorProfile'
import { UiButton } from '@/components/ui'
import ChatAssistSetting from './ChatAssistSetting.vue'
import CollapsibleSection from './CollapsibleSection.vue'
import NsfwModeSetting from './NsfwModeSetting.vue'
import PlayerPasswordPanel from './PlayerPasswordPanel.vue'
import PlayerPlaceLocaleSettings from './PlayerPlaceLocaleSettings.vue'
import PortalAccountLink from './PortalAccountLink.vue'
import QuotaOverageSettings from './QuotaOverageSettings.vue'
import SimpleImageProfilePicker from './SimpleImageProfilePicker.vue'
import TtsPregenSetting from './TtsPregenSetting.vue'
import VisualGenerationStyleSetting from './VisualGenerationStyleSetting.vue'
import WebNotificationSetting from './WebNotificationSetting.vue'

const { t } = useI18n()
const router = useRouter()
const { locale } = useLocale()
const {
  authEnabled,
  currentUser,
  isAdmin,
  cloudMode,
  logout,
} = useAuth()

// The place / timezone / language block owns its own drafts (its cloud and
// self-host branches diverge too far to share them); this section only
// hands it the loaded profile so both stay in sync with one GET.
const operatorProfile = ref<OperatorProfile | null>(null)
const displayNameDraft = ref('')
const displayNameLocked = ref(false)
const displayNameAliases = ref<string[]>([])
const displayNameSaving = ref(false)
const displayNameFeedback = ref<string | null>(null)
const currentStatusDraft = ref('')
const currentStatusSetAt = ref<string | null>(null)
const currentStatusSaving = ref(false)
const currentStatusLoading = ref(false)
const currentStatusFeedback = ref<string | null>(null)

const currentStatusSetAtLabel = computed(() => {
  if (!currentStatusSetAt.value || !currentUser.value?.timezone_id) return null
  return formatDateTime(
    currentStatusSetAt.value,
    locale.value,
    currentUser.value.timezone_id,
  )
})

function applyOperatorProfile(profile: OperatorProfile) {
  operatorProfile.value = profile
  if (!displayNameSaving.value) {
    displayNameDraft.value = profile.has_real_name ? profile.display_name : ''
  }
  displayNameLocked.value = profile.display_name_locked
  displayNameAliases.value = profile.aliases ?? []
  currentStatusDraft.value = profile.current_status ?? ''
  currentStatusSetAt.value = profile.current_status_set_at ?? null
}

async function loadOperatorProfile() {
  if (!currentUser.value) return
  currentStatusLoading.value = true
  currentStatusFeedback.value = null
  try {
    const profile = await getOperatorProfile()
    applyOperatorProfile(profile)
  } catch (err) {
    currentStatusFeedback.value = err instanceof Error
      ? t('common.errorWithDetail', { message: t('playerSidebar.currentStatus.loadFailed'), detail: err.message })
      : t('playerSidebar.currentStatus.loadFailed')
  } finally {
    currentStatusLoading.value = false
  }
}

async function saveDisplayName() {
  if (!currentUser.value) return
  const name = displayNameDraft.value.trim()
  if (!name) return
  displayNameSaving.value = true
  displayNameFeedback.value = null
  try {
    const profile = await updateOperatorProfile({ display_name: name })
    applyOperatorProfile(profile)
    window.dispatchEvent(new CustomEvent('kokoro:operator-profile-updated', {
      detail: profile,
    }))
    displayNameFeedback.value = t('playerSidebar.displayName.saved')
  } catch (err) {
    displayNameFeedback.value = err instanceof Error
      ? t('common.errorWithDetail', { message: t('playerSidebar.displayName.saveFailed'), detail: err.message })
      : t('playerSidebar.displayName.saveFailed')
  } finally {
    displayNameSaving.value = false
  }
}

async function saveCurrentStatus() {
  if (!currentUser.value) return
  currentStatusSaving.value = true
  currentStatusFeedback.value = null
  try {
    const profile = await updateOperatorProfile({
      current_status: currentStatusDraft.value.trim() || null,
    })
    applyOperatorProfile(profile)
    window.dispatchEvent(new CustomEvent('kokoro:operator-profile-updated', {
      detail: profile,
    }))
    currentStatusFeedback.value = profile.current_status
      ? t('playerSidebar.currentStatus.saved')
      : t('playerSidebar.currentStatus.cleared')
  } catch (err) {
    currentStatusFeedback.value = err instanceof Error
      ? t('common.errorWithDetail', { message: t('playerSidebar.currentStatus.saveFailed'), detail: err.message })
      : t('playerSidebar.currentStatus.saveFailed')
  } finally {
    currentStatusSaving.value = false
  }
}

async function clearCurrentStatus() {
  currentStatusDraft.value = ''
  await saveCurrentStatus()
}

function handleOperatorProfileUpdated(event: Event) {
  const profile = (event as CustomEvent<OperatorProfile>).detail
  if (profile) applyOperatorProfile(profile)
}

function handleLogout() {
  // Per-identity caches (the credit balance, and anything that subscribes
  // later) drop themselves off the back of the token change — see
  // `@/utils/identityLifecycle`. Doing it here only ever covered logout, and
  // left login / account switch on a shared browser leaking the previous
  // player's numbers.
  logout()
  router.replace({ name: 'login' })
}

watch(() => currentUser.value?.id, () => {
  void loadOperatorProfile()
}, { immediate: true })

onMounted(() => {
  window.addEventListener('kokoro:operator-profile-updated', handleOperatorProfileUpdated)
})

const webNotificationSetting = ref<InstanceType<typeof WebNotificationSetting> | null>(null)

async function flashWebNotification() {
  await webNotificationSetting.value?.flashReminder()
}

// provider 引導第一階段的閃光目標：左側設定頁的「管理者設定」入口。
const adminEntryRef = ref<ComponentPublicInstance | null>(null)
const adminEntryFlashing = ref(false)
let adminEntryFlashTimer: ReturnType<typeof setTimeout> | null = null

// 玩家頁「先設定 LLM provider」引導第一階段：切到個人設定後，把後台入口捲進視野
// 並閃一下，讓使用者記住下次要從這裡進管理後台設定 provider。
async function flashAdminEntry() {
  await nextTick()
  const el = adminEntryRef.value?.$el as HTMLElement | undefined
  if (!el) return
  el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  if (adminEntryFlashTimer) clearTimeout(adminEntryFlashTimer)
  adminEntryFlashing.value = true
  adminEntryFlashTimer = setTimeout(() => {
    adminEntryFlashing.value = false
    adminEntryFlashTimer = null
  }, 1700)
}

onUnmounted(() => {
  window.removeEventListener('kokoro:operator-profile-updated', handleOperatorProfileUpdated)
  if (adminEntryFlashTimer) clearTimeout(adminEntryFlashTimer)
})

defineExpose({ flashWebNotification, flashAdminEntry })
</script>

<template>
  <section v-if="currentUser" class="identity-section">
    <div class="display-name-field">
      <div class="display-name-field__head">
        <label class="field-label" for="operator-display-name">
          {{ t('playerSidebar.displayName.label') }}
        </label>
        <span v-if="displayNameLocked" class="display-name-field__badge">
          {{ t('playerSidebar.displayName.lockedBadge') }}
        </span>
      </div>
      <input
        id="operator-display-name"
        v-model="displayNameDraft"
        type="text"
        class="field-input"
        maxlength="80"
        :placeholder="t('playerSidebar.displayName.placeholder')"
        :disabled="displayNameSaving || currentStatusLoading"
      />
      <div class="display-name-actions">
        <UiButton
          variant="primary"
          size="sm"
          :loading="displayNameSaving"
          :disabled="currentStatusLoading || !displayNameDraft.trim()"
          @click="saveDisplayName"
        >
          {{ displayNameSaving ? t('playerSidebar.displayName.saving') : t('playerSidebar.displayName.save') }}
        </UiButton>
      </div>
      <p class="display-name-hint">{{ t('playerSidebar.displayName.hint') }}</p>
      <p v-if="displayNameLocked" class="display-name-hint">
        {{ t('playerSidebar.displayName.lockedHint') }}
      </p>
      <p class="display-name-aliases">
        <span class="display-name-aliases__label">
          {{ t('playerSidebar.displayName.aliasesLabel') }}
        </span>
        <template v-if="displayNameAliases.length">
          <span
            v-for="alias in displayNameAliases"
            :key="alias"
            class="display-name-aliases__chip"
          >{{ alias }}</span>
        </template>
        <span v-else class="display-name-aliases__empty">
          {{ t('playerSidebar.displayName.aliasesEmpty') }}
        </span>
      </p>
      <p v-if="displayNameFeedback" class="display-name-feedback">
        {{ displayNameFeedback }}
      </p>
    </div>
    <!-- 我的所在地／時區／語言。cloud 走玩家版（城市搜尋＋受控修改），
         self-host 分支與 G2 之前逐字相同（唯讀時區＋手填座標）。 -->
    <PlayerPlaceLocaleSettings
      :profile="operatorProfile"
      :disabled="currentStatusLoading"
    />
    <div class="current-status-field">
      <label class="field-label" for="operator-current-status">
        {{ t('playerSidebar.currentStatus.label') }}
      </label>
      <textarea
        id="operator-current-status"
        v-model="currentStatusDraft"
        class="field-textarea current-status-input"
        :placeholder="t('playerSidebar.currentStatus.placeholder')"
        :disabled="currentStatusSaving || currentStatusLoading"
        rows="2"
      />
      <div class="current-status-actions">
        <UiButton
          variant="primary"
          size="sm"
          :loading="currentStatusSaving"
          :disabled="currentStatusLoading"
          @click="saveCurrentStatus"
        >
          {{ currentStatusSaving ? t('playerSidebar.currentStatus.saving') : t('playerSidebar.currentStatus.save') }}
        </UiButton>
        <UiButton
          variant="ghost"
          size="sm"
          :disabled="currentStatusSaving || currentStatusLoading || !currentStatusDraft.trim()"
          @click="clearCurrentStatus"
        >
          {{ t('playerSidebar.currentStatus.clear') }}
        </UiButton>
      </div>
      <p class="current-status-hint">
        <span v-if="currentStatusSetAtLabel">
          {{ t('playerSidebar.currentStatus.setAt', { time: currentStatusSetAtLabel }) }}
        </span>
        <span v-else>{{ t('playerSidebar.currentStatus.hint') }}</span>
      </p>
      <p
        v-if="currentStatusFeedback"
        class="current-status-feedback"
      >
        {{ currentStatusFeedback }}
      </p>
    </div>
  </section>

  <!--
    Hosted identities live in the portal, and `require_self_host_mode`
    already 403s the change-password endpoint in cloud. Rendering the
    form there only walks the player into that 403 (plan U1-E-2).
  -->
  <CollapsibleSection
    v-if="authEnabled && !cloudMode"
    :title="t('playerSidebar.password.title')"
    :default-open="false"
  >
    <PlayerPasswordPanel :show-title="false" />
  </CollapsibleSection>

  <section class="settings-group">
    <h3 class="settings-group__title">{{ t('playerSidebar.settings.personalPreferencesTitle') }}</h3>
    <section class="voice-pregen-section">
      <ChatAssistSetting />
    </section>
    <!--
      Cloud mode never wires a TTS pregeneration service (billing point is
      the player's play button, not a background synthesize call), so the
      preference has nothing to control there. Hide it the same way
      NsfwModeSetting below hides a self-host-only toggle.
    -->
    <section v-if="!cloudMode" class="voice-pregen-section">
      <TtsPregenSetting />
    </section>
    <section class="voice-pregen-section">
      <WebNotificationSetting ref="webNotificationSetting" />
    </section>
    <section class="provider-section">
      <VisualGenerationStyleSetting />
    </section>
    <section v-if="!cloudMode" class="provider-section">
      <NsfwModeSetting />
    </section>
    <section v-if="isAdmin" class="provider-section">
      <SimpleImageProfilePicker />
    </section>
    <!-- Hosted overage switches. Deliberately not wrapped in a
         `.provider-section`: the component renders no nodes outside cloud
         mode, and an empty wrapper would still draw its divider — the
         self-host preferences group must stay byte-identical (plan AP4). -->
    <QuotaOverageSettings class="overage-section" />
  </section>

  <RouterLink
    v-if="isAdmin"
    ref="adminEntryRef"
    :to="{ name: 'admin-home' }"
    class="admin-settings-entry"
    :class="{ 'is-flashing': adminEntryFlashing }"
    :title="t('playerSidebar.admin.title')"
  >
    <span class="admin-settings-entry__title">{{ t('playerSidebar.admin.title') }}</span>
    <span class="admin-settings-entry__hint">{{ t('playerSidebar.admin.hint') }}</span>
  </RouterLink>

  <!-- Hosted 玩家的回程動線：放在登出旁邊，因為兩者是同一個「離開這個
       遊戲畫面」的意圖。self-host 下這個元件不輸出任何節點（連容器都沒有），
       個人設定面在自架站的渲染結果與改動前逐字元相同。 -->
  <PortalAccountLink class="portal-account-entry" />

  <section v-if="authEnabled" class="logout-section">
    <UiButton
      variant="danger"
      size="md"
      block
      @click="handleLogout"
    >
      {{ t('playerSidebar.actions.logout') }}
    </UiButton>
  </section>
</template>

<style scoped>
.identity-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.04);
}
/* .identity-section__row / __hint live in PlayerPlaceLocaleSettings.vue
   alongside the markup that uses them. */

.display-name-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-bottom: 8px;
  border-bottom: 1px dashed var(--color-border);
}
.display-name-field__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.display-name-field__badge {
  font-size: 10px;
  font-weight: 600;
  color: var(--color-primary-light);
  border: 1px solid rgba(232, 155, 133, 0.4);
  border-radius: 999px;
  padding: 1px 8px;
  background: rgba(232, 155, 133, 0.08);
}
.display-name-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.display-name-hint,
.display-name-feedback,
.display-name-aliases {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}
.display-name-feedback {
  color: #7dc49a;
}
.display-name-aliases__chip {
  display: inline-block;
  margin: 0 4px 4px 0;
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text);
  font-size: 11px;
}
.display-name-aliases__label {
  color: var(--color-text-secondary);
}

.current-status-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: 8px;
  border-top: 1px dashed var(--color-border);
}

.current-status-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.current-status-input {
  min-height: 58px;
}

.current-status-hint,
.current-status-feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.current-status-feedback {
  color: #7dc49a;
}

.settings-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.settings-group__title {
  margin: 0;
  color: var(--color-primary-light);
  font-size: var(--font-xs);
  font-weight: 700;
}

.provider-section,
.voice-pregen-section {
  padding-top: 10px;
  border-top: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.admin-settings-entry {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  color: var(--color-text-secondary);
  text-decoration: none;
  font-size: var(--font-xs);
  border: 1px dashed var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}

.admin-settings-entry:hover {
  color: var(--color-text);
  border-color: rgba(232, 155, 133, 0.35);
  background: rgba(232, 155, 133, 0.06);
}

/* provider 引導第一階段：用 outline 脈衝高亮後台入口，不撐版面、不位移。 */
.admin-settings-entry.is-flashing {
  animation: admin-entry-flash 0.85s ease-in-out 2;
}

@keyframes admin-entry-flash {
  0%, 100% {
    outline: 2px solid rgba(240, 168, 104, 0);
    outline-offset: 3px;
    background: transparent;
  }
  50% {
    outline: 2px solid rgba(240, 168, 104, 0.7);
    outline-offset: 3px;
    background: rgba(240, 168, 104, 0.12);
  }
}

.admin-settings-entry__title {
  font-weight: 600;
  color: var(--color-text);
}

.admin-settings-entry__hint {
  line-height: 1.45;
}

/* Matches the divider the sibling preference blocks draw, without an empty
   wrapper that would render on self-host where the component is silent. */
.overage-section {
  padding-top: 10px;
  border-top: 1px solid var(--color-border);
}

.portal-account-entry {
  margin-top: var(--space-2);
}

.logout-section {
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}
</style>
