<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  type AuthUser,
  changePassword,
  createUser,
  deleteUser,
  listUsers,
  setUserAdmin,
} from '@/utils/api/auth'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  UiBadge,
  UiButton,
  UiCard,
  UiInput,
  UiSelect,
  UiSection,
} from '@/components/ui'

// Admin-only Users page (MULTI_USER_AUTH_PLAN Batch 7 / review P2-3).
// Provides minimal CRUD on /auth/users so an admin can add or remove
// other accounts without curl. The auth router on the backend gates
// every endpoint touched here with `Depends(require_admin)`; the page
// itself lives behind the standard /admin layout so non-admin users
// either bounce off the layout's guard or hit a backend 403.

const { t } = useI18n()
const { locale, supported } = useLocale()
const { timeZone, timezoneOptions } = useTimezone()
const confirmDialog = useConfirmDialog()
const primaryLanguageOptions = computed(() =>
  supported.value.map((item) => ({
    value: item.code,
    label: item.label,
  })),
)

const users = ref<AuthUser[]>([])
const loading = ref(false)
const error = ref<string | null>(null)

const form = ref<{
  email: string
  password: string
  display_name: string
  is_admin: boolean
  primary_language: string
  timezone_id: string
  country_code: string
  latitude: string | number
  longitude: string | number
  location_label: string
}>({
  email: '',
  password: '',
  display_name: '',
  is_admin: false,
  primary_language: locale.value,
  timezone_id: timeZone.value,
  country_code: '',
  latitude: '',
  longitude: '',
  location_label: '',
})
const formError = ref<string | null>(null)
const formBusy = ref(false)

const passwordResetTarget = ref<string | null>(null)
const passwordResetValue = ref('')
const passwordResetError = ref<string | null>(null)
const passwordResetBusy = ref(false)

function fallbackError(err: unknown, fallbackKey: string): string {
  return err instanceof Error && err.message ? err.message : t(fallbackKey)
}

function languageLabel(language: string): string {
  return supported.value.find((item) => item.code === language)?.label ?? language
}

function locationLabel(user: AuthUser): string {
  return user.location_label || user.country_code || t('common.fallback.notSet')
}

function createLocationPayload() {
  const payload: {
    country_code?: string
    latitude?: number
    longitude?: number
    location_label?: string
  } = {}
  if (form.value.country_code.trim()) {
    payload.country_code = form.value.country_code.trim()
  }
  const latitude = String(form.value.latitude ?? '').trim()
  const longitude = String(form.value.longitude ?? '').trim()
  if (latitude) {
    payload.latitude = Number(latitude)
  }
  if (longitude) {
    payload.longitude = Number(longitude)
  }
  if (form.value.location_label.trim()) {
    payload.location_label = form.value.location_label.trim()
  }
  return payload
}

async function refresh() {
  loading.value = true
  error.value = null
  try {
    users.value = await listUsers()
  } catch (err) {
    error.value = fallbackError(err, 'admin.usersAdmin.errors.listFailed')
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  formError.value = null
  if (!form.value.email.trim() || !form.value.password.trim() || !form.value.display_name.trim()) {
    formError.value = t('admin.usersAdmin.errors.requiredFields')
    return
  }
  formBusy.value = true
  try {
    await createUser({
      email: form.value.email.trim(),
      password: form.value.password,
      display_name: form.value.display_name.trim(),
      is_admin: form.value.is_admin,
      primary_language: form.value.primary_language,
      timezone_id: form.value.timezone_id,
      ...createLocationPayload(),
    })
    form.value = {
      email: '',
      password: '',
      display_name: '',
      is_admin: false,
      primary_language: locale.value,
      timezone_id: timeZone.value,
      country_code: '',
      latitude: '',
      longitude: '',
      location_label: '',
    }
    await refresh()
  } catch (err) {
    // Backend uses 401 InvalidCredentials for invalid email/password
    // shape; 409 for existing email. Surface both verbatim so the
    // admin sees why the create failed.
    formError.value = fallbackError(err, 'admin.usersAdmin.errors.createFailed')
  } finally {
    formBusy.value = false
  }
}

async function handleDelete(user: AuthUser) {
  if (!await confirmDialog({
    content: t('admin.usersAdmin.confirmDelete', {
      name: user.display_name,
      identity: user.email ?? user.id,
    }),
    okText: t('common.actions.delete'),
    danger: true,
  })) {
    return
  }
  try {
    await deleteUser(user.id)
    await refresh()
  } catch (err) {
    error.value = fallbackError(err, 'admin.usersAdmin.errors.deleteFailed')
  }
}

async function handleToggleAdmin(user: AuthUser) {
  // Recovery for a second account created without Grant admin: admin
  // surfaces (provider keys / BYOK, models, site settings) gate on this flag.
  if (user.is_admin) {
    if (!await confirmDialog({
      content: t('admin.usersAdmin.confirmRevokeAdmin', { name: user.display_name }),
      okText: t('admin.usersAdmin.revokeAdminAction'),
      danger: true,
    })) {
      return
    }
  }
  try {
    await setUserAdmin(user.id, !user.is_admin)
    await refresh()
  } catch (err) {
    // Backend 403 guards the last-admin demote; surface the message verbatim.
    error.value = fallbackError(err, 'admin.usersAdmin.errors.setAdminFailed')
  }
}

function startPasswordReset(user: AuthUser) {
  passwordResetTarget.value = user.id
  passwordResetValue.value = ''
  passwordResetError.value = null
}

function cancelPasswordReset() {
  passwordResetTarget.value = null
  passwordResetValue.value = ''
  passwordResetError.value = null
}

async function submitPasswordReset() {
  if (!passwordResetTarget.value) return
  if (!passwordResetValue.value.trim()) {
    passwordResetError.value = t('admin.usersAdmin.errors.passwordRequired')
    return
  }
  passwordResetBusy.value = true
  try {
    await changePassword(passwordResetTarget.value, passwordResetValue.value)
    cancelPasswordReset()
  } catch (err) {
    passwordResetError.value = fallbackError(err, 'admin.usersAdmin.errors.resetFailed')
  } finally {
    passwordResetBusy.value = false
  }
}

onMounted(refresh)
</script>

<template>
  <div class="users-admin">
    <header class="users-admin__header">
      <h1>{{ t('admin.usersAdmin.title') }}</h1>
      <p class="users-admin__lead">
        {{ t('admin.usersAdmin.subtitle') }}
      </p>
    </header>

    <UiSection :title="t('admin.usersAdmin.createTitle')">
      <form class="users-admin__form" @submit.prevent="handleCreate">
        <div class="users-admin__form-grid">
          <UiInput
            v-model="form.email"
            :label="t('admin.usersAdmin.emailLabel')"
            placeholder="user@example.com"
          />
          <UiInput
            v-model="form.password"
            :label="t('admin.usersAdmin.passwordLabel')"
            type="password"
            :placeholder="t('admin.usersAdmin.passwordPlaceholder')"
          />
          <UiInput
            v-model="form.display_name"
            :label="t('admin.usersAdmin.displayNameLabel')"
            :placeholder="t('admin.usersAdmin.displayNamePlaceholder')"
          />
          <UiSelect
            v-model="form.primary_language"
            :label="t('locale.primaryLanguage.label')"
            :hint="t('locale.primaryLanguage.readonlyExplain')"
            :options="primaryLanguageOptions"
            required
          />
          <UiSelect
            v-model="form.timezone_id"
            :label="t('locale.timezone.label')"
            :hint="t('locale.timezone.creationHint')"
            :options="timezoneOptions"
            required
          />
          <UiInput
            v-model="form.location_label"
            :label="t('locale.location.label')"
            :hint="t('locale.location.creationHint')"
            :placeholder="t('locale.location.labelPlaceholder')"
          />
          <UiInput
            v-model="form.country_code"
            :label="t('locale.location.countryCode')"
            :placeholder="t('locale.location.countryPlaceholder')"
          />
          <UiInput
            v-model="form.latitude"
            :label="t('locale.location.latitude')"
            type="number"
            step="0.000001"
            :placeholder="t('locale.location.latitudePlaceholder')"
          />
          <UiInput
            v-model="form.longitude"
            :label="t('locale.location.longitude')"
            type="number"
            step="0.000001"
            :placeholder="t('locale.location.longitudePlaceholder')"
          />
        </div>
        <label class="users-admin__admin-check">
          <input type="checkbox" v-model="form.is_admin" />
          <span>{{ t('admin.usersAdmin.grantAdmin') }}</span>
        </label>
        <p class="users-admin__admin-hint">{{ t('admin.usersAdmin.grantAdminHint') }}</p>
        <div class="users-admin__form-actions">
          <UiButton type="submit" variant="primary" :loading="formBusy">
            {{ t('admin.usersAdmin.createAction') }}
          </UiButton>
        </div>
        <p v-if="formError" class="users-admin__error">{{ formError }}</p>
      </form>
    </UiSection>

    <UiSection :title="t('admin.usersAdmin.listTitle')">
      <UiButton variant="ghost" size="sm" @click="refresh" :loading="loading">
        {{ t('common.actions.refresh') }}
      </UiButton>
      <p v-if="error" class="users-admin__error">{{ error }}</p>
      <div class="users-admin__list">
        <UiCard v-for="user in users" :key="user.id" class="users-admin__card">
          <div class="users-admin__card-header">
            <div>
              <strong>{{ user.display_name }}</strong>
              <UiBadge v-if="user.is_admin" variant="success">admin</UiBadge>
            </div>
            <span class="users-admin__id">{{ user.email ?? user.id }}</span>
          </div>
          <div class="users-admin__meta">
            <span>{{ t('locale.primaryLanguage.label') }}: {{ languageLabel(user.primary_language) }}</span>
            <span>{{ t('locale.timezone.label') }}: {{ user.timezone_id }}</span>
            <span>{{ t('locale.location.label') }}: {{ locationLabel(user) }}</span>
          </div>
          <div v-if="passwordResetTarget === user.id" class="users-admin__reset">
            <UiInput
              v-model="passwordResetValue"
              type="password"
              :label="t('admin.usersAdmin.newPasswordLabel')"
              :placeholder="t('admin.usersAdmin.passwordPlaceholder')"
            />
            <div class="users-admin__reset-actions">
              <UiButton
                size="sm" variant="primary"
                :loading="passwordResetBusy"
                @click="submitPasswordReset"
              >
                {{ t('common.actions.confirm') }}
              </UiButton>
              <UiButton size="sm" variant="ghost" @click="cancelPasswordReset">
                {{ t('common.actions.cancel') }}
              </UiButton>
            </div>
            <p v-if="passwordResetError" class="users-admin__error">{{ passwordResetError }}</p>
          </div>
          <div v-else class="users-admin__card-actions">
            <UiButton size="sm" variant="ghost" @click="handleToggleAdmin(user)">
              {{ user.is_admin
                ? t('admin.usersAdmin.revokeAdminAction')
                : t('admin.usersAdmin.makeAdminAction') }}
            </UiButton>
            <UiButton size="sm" variant="ghost" @click="startPasswordReset(user)">
              {{ t('admin.usersAdmin.resetPasswordAction') }}
            </UiButton>
            <UiButton size="sm" variant="danger" @click="handleDelete(user)">
              {{ t('common.actions.delete') }}
            </UiButton>
          </div>
        </UiCard>
        <p v-if="!loading && users.length === 0" class="users-admin__empty">
          {{ t('admin.usersAdmin.empty') }}
        </p>
      </div>
    </UiSection>
  </div>
</template>

<style scoped>
.users-admin {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  max-width: 900px;
}
.users-admin__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.users-admin__lead {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.users-admin__form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.users-admin__form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: var(--space-3);
}
.users-admin__admin-check {
  display: flex;
  gap: var(--space-2);
  align-items: center;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
}
.users-admin__admin-hint {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.users-admin__form-actions {
  display: flex;
  justify-content: flex-end;
}
.users-admin__list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  margin-top: var(--space-3);
}
.users-admin__card-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: var(--space-2);
}
.users-admin__card-header > div {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.users-admin__id {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.users-admin__meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  margin-top: var(--space-2);
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
}
.users-admin__card-actions,
.users-admin__reset-actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-3);
}
.users-admin__reset {
  margin-top: var(--space-3);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.users-admin__error {
  margin: 0;
  color: var(--color-danger);
  font-size: var(--font-sm);
}
.users-admin__empty {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
}
</style>
