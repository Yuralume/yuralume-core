<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { notification } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { UiBadge, UiButton, UiSection } from '@/components/ui'
import { useLocale } from '@/composables/useLocale'
import { useTimezone } from '@/composables/useTimezone'
import {
  getAppSettingsGroup,
  putAppSettingsGroup,
  type CharacterFreezeRuntimeConfig,
} from '@/utils/api/appSettings'
import {
  freezeCharacter,
  getCharacterFreezeOverview,
  unfreezeCharacter,
  type AdminCharacterOverviewRow,
} from '@/utils/api/adminCharacters'
import { formatDateTime, formatRelativeTime } from '@/i18n/formatters'

// Site-wide character freeze admin page. Two independent sections:
//   1. Auto-freeze threshold (generic app-settings group `character_freeze`,
//      same read/write pattern as SiteSettingsAdminPage).
//   2. Full character roster with last-active time + manual freeze/unfreeze,
//      backed by the dedicated admin characters overview endpoint. The
//      backend already returns rows sorted "most-stale first"; we render
//      them as-is and only add a client-side sort toggle on top.

const { t } = useI18n()
const { locale } = useLocale()
const { timeZone } = useTimezone()

const settingsLoading = ref(true)
const settingsSaving = ref(false)
const autoFreeze = reactive<CharacterFreezeRuntimeConfig>({
  auto_freeze_enabled: false,
  idle_days_threshold: 30,
})

async function loadAutoFreezeSettings(): Promise<void> {
  settingsLoading.value = true
  try {
    const values = await getAppSettingsGroup<CharacterFreezeRuntimeConfig>('character_freeze')
    Object.assign(autoFreeze, values)
  } catch (err) {
    notification.error({
      message: t('admin.characterFreeze.autoFreeze.loadFailed'),
      description: err instanceof Error ? err.message : String(err),
    })
  } finally {
    settingsLoading.value = false
  }
}

async function saveAutoFreezeSettings(): Promise<void> {
  settingsSaving.value = true
  try {
    await putAppSettingsGroup('character_freeze', {
      auto_freeze_enabled: autoFreeze.auto_freeze_enabled,
      idle_days_threshold: autoFreeze.idle_days_threshold,
    })
    notification.success({ message: t('admin.characterFreeze.autoFreeze.saved'), duration: 2 })
  } catch (err) {
    const detail =
      (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? (err instanceof Error ? err.message : String(err))
    notification.error({
      message: t('admin.characterFreeze.autoFreeze.saveFailed'),
      description: String(detail),
      duration: 6,
    })
  } finally {
    settingsSaving.value = false
  }
}

type SortMode = 'default' | 'name'

const listLoading = ref(true)
const characters = ref<AdminCharacterOverviewRow[]>([])
const busyIds = reactive<Record<string, boolean>>({})
const sortMode = ref<SortMode>('default')

const sortedCharacters = computed(() => {
  if (sortMode.value === 'name') {
    return [...characters.value].sort((a, b) => a.name.localeCompare(b.name))
  }
  return characters.value
})

async function loadCharacters(): Promise<void> {
  listLoading.value = true
  try {
    const overview = await getCharacterFreezeOverview()
    characters.value = overview.characters
  } catch (err) {
    notification.error({
      message: t('admin.characterFreeze.list.loadFailed'),
      description: err instanceof Error ? err.message : String(err),
    })
  } finally {
    listLoading.value = false
  }
}

function lastActiveLabel(row: AdminCharacterOverviewRow): string {
  if (!row.last_active_at) return t('admin.characterFreeze.list.neverActive')
  return formatRelativeTime(row.last_active_at, locale.value)
}

function lastActiveTitle(row: AdminCharacterOverviewRow): string | undefined {
  if (!row.last_active_at) return undefined
  return formatDateTime(row.last_active_at, locale.value, timeZone.value)
}

function frozenAtLabel(row: AdminCharacterOverviewRow): string | undefined {
  if (!row.frozen || !row.frozen_at) return undefined
  return t('admin.characterFreeze.list.frozenAt', {
    time: formatDateTime(row.frozen_at, locale.value, timeZone.value),
  })
}

function frozenReasonLabel(row: AdminCharacterOverviewRow): string | undefined {
  if (!row.frozen || !row.frozen_reason) return undefined
  // Known reasons get a localized label; an unrecognized value degrades to
  // the raw string rather than a missing-key placeholder.
  const key = `admin.characterFreeze.list.reason.${row.frozen_reason}`
  const label = t(key)
  return label === key ? row.frozen_reason : label
}

async function toggleFreeze(row: AdminCharacterOverviewRow): Promise<void> {
  busyIds[row.id] = true
  try {
    const result = row.frozen
      ? await unfreezeCharacter(row.id)
      : await freezeCharacter(row.id)
    const target = characters.value.find((c) => c.id === row.id)
    if (target) {
      target.frozen = result.frozen
      target.frozen_at = result.frozen_at
      target.frozen_reason = result.frozen_reason
    }
  } catch (err) {
    const failKey = row.frozen
      ? 'admin.characterFreeze.list.unfreezeFailed'
      : 'admin.characterFreeze.list.freezeFailed'
    const detail =
      (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      ?? (err instanceof Error ? err.message : String(err))
    notification.error({
      message: t(failKey),
      description: String(detail),
      duration: 6,
    })
  } finally {
    busyIds[row.id] = false
  }
}

onMounted(() => {
  void loadAutoFreezeSettings()
  void loadCharacters()
})
</script>

<template>
  <div class="character-freeze">
    <header class="character-freeze__header">
      <div>
        <h1>{{ t('admin.characterFreeze.title') }}</h1>
        <p class="character-freeze__subtitle">{{ t('admin.characterFreeze.subtitle') }}</p>
      </div>
      <UiBadge variant="primary">{{ t('admin.characterFreeze.badge') }}</UiBadge>
    </header>

    <UiSection :title="t('admin.characterFreeze.autoFreeze.title')" bordered>
      <p class="field-hint">{{ t('admin.characterFreeze.autoFreeze.hint') }}</p>
      <p v-if="settingsLoading" class="character-freeze__loading">{{ t('common.state.loading') }}</p>
      <template v-else>
        <label class="field-label character-freeze__check">
          <input v-model="autoFreeze.auto_freeze_enabled" type="checkbox" />
          {{ t('admin.characterFreeze.autoFreeze.enabled') }}
        </label>
        <label class="field-label character-freeze__idle-days">
          {{ t('admin.characterFreeze.autoFreeze.idleDays') }}
          <input
            v-model.number="autoFreeze.idle_days_threshold"
            class="field-input"
            type="number"
            min="1"
          />
        </label>
        <UiButton
          variant="primary"
          :loading="settingsSaving"
          @click="saveAutoFreezeSettings"
        >
          {{ t('admin.characterFreeze.autoFreeze.save') }}
        </UiButton>
      </template>
    </UiSection>

    <UiSection :title="t('admin.characterFreeze.list.title')" bordered>
      <div class="character-freeze__list-toolbar">
        <label class="field-label character-freeze__sort">
          <select v-model="sortMode" class="field-select">
            <option value="default">{{ t('admin.characterFreeze.list.colLastActive') }}</option>
            <option value="name">{{ t('admin.characterFreeze.list.colName') }}</option>
          </select>
        </label>
        <UiButton variant="ghost" size="sm" :loading="listLoading" @click="loadCharacters">
          {{ t('common.actions.refresh') }}
        </UiButton>
      </div>

      <p v-if="listLoading" class="character-freeze__loading">{{ t('common.state.loading') }}</p>
      <p v-else-if="sortedCharacters.length === 0" class="character-freeze__empty">
        {{ t('admin.characterFreeze.list.empty') }}
      </p>
      <div v-else class="character-freeze__table-wrap">
        <table class="character-freeze__table">
          <thead>
            <tr>
              <th>{{ t('admin.characterFreeze.list.colName') }}</th>
              <th>{{ t('admin.characterFreeze.list.colOwner') }}</th>
              <th>{{ t('admin.characterFreeze.list.colLastActive') }}</th>
              <th>{{ t('admin.characterFreeze.list.colStatus') }}</th>
              <th>{{ t('admin.characterFreeze.list.colActions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in sortedCharacters" :key="row.id">
              <td>{{ row.name }}</td>
              <td class="character-freeze__owner">{{ row.owner_user_id }}</td>
              <td :title="lastActiveTitle(row)">{{ lastActiveLabel(row) }}</td>
              <td>
                <UiBadge :variant="row.frozen ? 'default' : 'success'">
                  {{ row.frozen
                    ? t('admin.characterFreeze.list.statusFrozen')
                    : t('admin.characterFreeze.list.statusActive') }}
                </UiBadge>
                <span v-if="frozenReasonLabel(row)" class="character-freeze__reason">
                  {{ frozenReasonLabel(row) }}
                </span>
                <span v-if="frozenAtLabel(row)" class="character-freeze__frozen-at">
                  {{ frozenAtLabel(row) }}
                </span>
              </td>
              <td>
                <UiButton
                  size="sm"
                  :variant="row.frozen ? 'secondary' : 'danger'"
                  :loading="busyIds[row.id]"
                  @click="toggleFreeze(row)"
                >
                  {{ row.frozen
                    ? t('admin.characterFreeze.list.unfreeze')
                    : t('admin.characterFreeze.list.freeze') }}
                </UiButton>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </UiSection>
  </div>
</template>

<style scoped>
.character-freeze {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-width: 1100px;
}
.character-freeze__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
}
.character-freeze__header h1 {
  margin: 0 0 var(--space-1);
  font-size: var(--font-xl);
}
.character-freeze__subtitle {
  margin: 0;
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.character-freeze__loading,
.character-freeze__empty {
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
}
.character-freeze__check {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.character-freeze__idle-days {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  max-width: 220px;
  margin-bottom: var(--space-3);
}
.character-freeze__list-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.character-freeze__sort {
  min-width: 160px;
}
.character-freeze__table-wrap {
  overflow-x: auto;
}
.character-freeze__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-sm);
}
.character-freeze__table th,
.character-freeze__table td {
  padding: var(--space-2) var(--space-3);
  text-align: left;
  border-bottom: 1px solid var(--color-border);
  vertical-align: middle;
}
.character-freeze__table th {
  color: var(--color-text-secondary);
  font-weight: 500;
  white-space: nowrap;
}
.character-freeze__owner {
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
}
.character-freeze__reason {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.character-freeze__frozen-at {
  display: block;
  margin-top: var(--space-1);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
</style>
