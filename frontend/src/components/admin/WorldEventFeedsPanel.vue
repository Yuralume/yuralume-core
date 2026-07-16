<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { notification } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import { UiButton, UiSection } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  createWorldEventFeed,
  deleteWorldEventFeed,
  listWorldEventFeeds,
  updateWorldEventFeed,
  type FeedCreatePayload,
  type WorldEventFeed,
} from '@/utils/api/worldEventFeeds'

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const feeds = ref<WorldEventFeed[]>([])
const loading = ref(true)
const busy = reactive<Record<string, boolean>>({})

const draft = reactive<FeedCreatePayload>({
  id: '',
  name: '',
  feed_url: '',
  category: 'news',
  locale: 'zh-TW',
  enabled: true,
})

async function load(): Promise<void> {
  loading.value = true
  try {
    feeds.value = (await listWorldEventFeeds()).sources
  } catch (err) {
    notification.error({
      message: t('admin.worldFeeds.loadFailed'),
      description: err instanceof Error ? err.message : String(err),
    })
  } finally {
    loading.value = false
  }
}

function apiError(err: unknown): string {
  return (
    (err as { response?: { data?: { detail?: string } } })?.response?.data
      ?.detail ?? (err instanceof Error ? err.message : String(err))
  )
}

async function addFeed(): Promise<void> {
  if (!draft.id.trim() || !draft.feed_url.trim()) {
    notification.warning({ message: t('admin.worldFeeds.needIdUrl'), duration: 3 })
    return
  }
  busy.__new = true
  try {
    await createWorldEventFeed({ ...draft })
    notification.success({ message: t('admin.worldFeeds.added'), duration: 2 })
    draft.id = ''
    draft.name = ''
    draft.feed_url = ''
    await load()
  } catch (err) {
    notification.error({
      message: t('admin.worldFeeds.addFailed'),
      description: String(apiError(err)),
      duration: 6,
    })
  } finally {
    busy.__new = false
  }
}

async function toggle(feed: WorldEventFeed): Promise<void> {
  busy[feed.id] = true
  try {
    await updateWorldEventFeed(feed.id, { enabled: !feed.enabled })
    await load()
  } catch (err) {
    notification.error({
      message: t('admin.worldFeeds.updateFailed'),
      description: String(apiError(err)),
    })
  } finally {
    busy[feed.id] = false
  }
}

async function remove(feed: WorldEventFeed): Promise<void> {
  const ok = await confirmDialog({
    title: t('admin.worldFeeds.deleteConfirm', { id: feed.id }),
    danger: true,
  })
  if (!ok) return
  busy[feed.id] = true
  try {
    await deleteWorldEventFeed(feed.id)
    await load()
  } catch (err) {
    notification.error({
      message: t('admin.worldFeeds.deleteFailed'),
      description: String(apiError(err)),
    })
  } finally {
    busy[feed.id] = false
  }
}

onMounted(load)
</script>

<template>
  <UiSection :title="t('admin.worldFeeds.title')" bordered>
    <p class="field-hint">{{ t('admin.worldFeeds.hint') }}</p>

    <!-- Add feed -->
    <div class="world-feeds__add">
      <label class="field-label">
        {{ t('admin.worldFeeds.id') }}
        <input v-model="draft.id" class="field-input" type="text" placeholder="mynews" />
      </label>
      <label class="field-label">
        {{ t('admin.worldFeeds.url') }}
        <input v-model="draft.feed_url" class="field-input" type="text" placeholder="https://example.com/rss" />
      </label>
      <label class="field-label">
        {{ t('admin.worldFeeds.category') }}
        <input v-model="draft.category" class="field-input" type="text" placeholder="news" />
      </label>
      <UiButton variant="primary" :loading="busy.__new" @click="addFeed">
        {{ t('admin.worldFeeds.add') }}
      </UiButton>
    </div>

    <p v-if="loading" class="field-hint">{{ t('common.loading') }}</p>
    <p v-else-if="feeds.length === 0" class="field-hint">
      {{ t('admin.worldFeeds.empty') }}
    </p>
    <table v-else class="world-feeds__table">
      <thead>
        <tr>
          <th>{{ t('admin.worldFeeds.colId') }}</th>
          <th>{{ t('admin.worldFeeds.colUrl') }}</th>
          <th>{{ t('admin.worldFeeds.colCategory') }}</th>
          <th>{{ t('admin.worldFeeds.colStatus') }}</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="feed in feeds" :key="feed.id">
          <td>{{ feed.id }}</td>
          <td class="world-feeds__url">{{ feed.feed_url }}</td>
          <td>{{ feed.category }}</td>
          <td>{{ feed.health_status }}</td>
          <td class="world-feeds__actions">
            <UiButton size="sm" variant="ghost" :loading="busy[feed.id]" @click="toggle(feed)">
              {{ feed.enabled ? t('admin.worldFeeds.disable') : t('admin.worldFeeds.enable') }}
            </UiButton>
            <UiButton size="sm" variant="danger" :loading="busy[feed.id]" @click="remove(feed)">
              {{ t('admin.worldFeeds.delete') }}
            </UiButton>
          </td>
        </tr>
      </tbody>
    </table>
  </UiSection>
</template>

<style scoped>
.world-feeds__add {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)) auto;
  gap: var(--space-3);
  align-items: end;
  margin-bottom: var(--space-4);
}
.world-feeds__table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--font-sm);
}
.world-feeds__table th,
.world-feeds__table td {
  text-align: left;
  padding: var(--space-2);
  border-bottom: 1px solid var(--color-border);
}
.world-feeds__url {
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.world-feeds__actions {
  display: flex;
  gap: var(--space-2);
}
</style>
