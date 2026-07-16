<script setup lang="ts">
/**
 * 玩家側的 read-only 待回覆訊息卡片。
 *
 * 只列出當前角色「queued / resolving」的待回覆，告訴玩家「我有 N
 * 則訊息會晚點補回，預計 X 之後」。dispatch / dismiss / 重排程都是
 * admin 行為，這裡不暴露。
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { useI18n } from 'vue-i18n'
import {
  listOpenPendingFollowUps,
  type PendingFollowUp,
} from '@/utils/api/pendingFollowUps'

const props = defineProps<{
  characterId: string | null
  showAdminLink?: boolean
}>()

const { t } = useI18n()

const rows = ref<PendingFollowUp[]>([])
const loading = ref(false)
const errorMsg = ref<string | null>(null)

const STATUS_LABEL = computed<Record<PendingFollowUp['status'], string>>(() => ({
  queued: t('playerFollowUps.statusQueued'),
  resolving: t('playerFollowUps.statusResolving'),
  resolved: t('playerFollowUps.statusResolved'),
  cancelled: t('playerFollowUps.statusCancelled'),
}))

const visibleRows = computed(() =>
  rows.value.filter((r) => r.status === 'queued' || r.status === 'resolving'),
)

function formatRelative(iso: string): string {
  const date = new Date(iso)
  const diffMs = date.getTime() - Date.now()
  const absSec = Math.abs(diffMs) / 1000
  const future = diffMs > 0
  if (absSec < 60) {
    return future ? t('playerFollowUps.relImminent') : t('playerFollowUps.relJust')
  }
  if (absSec < 3600) {
    const n = Math.round(absSec / 60)
    return future ? t('playerFollowUps.relMinutesAhead', { n }) : t('playerFollowUps.relMinutesAgo', { n })
  }
  if (absSec < 86400) {
    const n = Math.round(absSec / 3600)
    return future ? t('playerFollowUps.relHoursAhead', { n }) : t('playerFollowUps.relHoursAgo', { n })
  }
  const n = Math.round(absSec / 86400)
  return future ? t('playerFollowUps.relDaysAhead', { n }) : t('playerFollowUps.relDaysAgo', { n })
}

async function reload() {
  if (!props.characterId) {
    rows.value = []
    return
  }
  loading.value = true
  errorMsg.value = null
  try {
    rows.value = await listOpenPendingFollowUps(props.characterId)
  } catch (err) {
    errorMsg.value = err instanceof Error ? err.message : t('common.errors.loadFailed', { reason: t('common.errors.unknown') })
    rows.value = []
  } finally {
    loading.value = false
  }
}

watch(() => props.characterId, () => { void reload() }, { immediate: true })

let pollTimer: ReturnType<typeof setInterval> | null = null
watch(() => props.characterId, (id) => {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
  if (id) {
    pollTimer = setInterval(() => { void reload() }, 30_000)
  }
}, { immediate: true })

onBeforeUnmount(() => {
  if (pollTimer !== null) clearInterval(pollTimer)
})
</script>

<template>
  <div class="player-follow-ups-card">
    <header class="card-head">
      <span class="title-text">{{ t('playerFollowUps.title') }}</span>
      <RouterLink
        v-if="characterId && showAdminLink"
        :to="{ name: 'admin-follow-ups', query: { character: characterId } }"
        class="admin-link"
        :title="t('playerFollowUps.adminTitle')"
      >{{ t('playerFollowUps.adminLink') }}</RouterLink>
    </header>

    <div v-if="loading" class="state-row">{{ t('common.state.loading') }}</div>
    <div v-else-if="errorMsg" class="state-row state-row--err">{{ errorMsg }}</div>
    <div v-else-if="!characterId" class="state-row">{{ t('playerFollowUps.selectCharacter') }}</div>
    <div v-else-if="visibleRows.length === 0" class="state-row state-row--quiet">
      {{ t('playerFollowUps.empty') }}
    </div>

    <ul v-else class="follow-up-list">
      <li
        v-for="row in visibleRows"
        :key="row.id"
        class="follow-up-item"
      >
        <div class="follow-up-status">
          <span :class="['status-chip', `status-${row.status}`]">
            {{ STATUS_LABEL[row.status] }}
          </span>
          <span class="due-text">{{ t('playerFollowUps.dueText', { time: formatRelative(row.scheduled_for) }) }}</span>
        </div>
        <div class="follow-up-brief">{{ t('common.quoted', { text: row.brief_reply }) }}</div>
        <div v-if="row.messages.length > 0" class="follow-up-msgs">
          {{ t('playerFollowUps.msgsHint', { count: row.messages.length }) }}
          <span class="msg-preview">
            {{ row.messages[row.messages.length - 1].content.slice(0, 60) }}
            <span v-if="row.messages[row.messages.length - 1].content.length > 60">…</span>
          </span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.player-follow-ups-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--card-padding);
  background: var(--card-bg);
  border: var(--card-border);
  border-radius: var(--card-radius);
}
.card-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.title-text {
  font-size: var(--font-md);
  font-weight: 600;
  color: var(--color-primary-light);
}
.admin-link {
  font-size: var(--font-xs);
  color: var(--color-primary);
  text-decoration: none;
}
.admin-link:hover {
  text-decoration: underline;
}
.state-row {
  font-size: var(--font-sm);
  color: var(--color-text-secondary);
  padding: var(--space-2) 0;
  text-align: center;
}
.state-row--quiet {
  font-size: var(--font-xs);
  opacity: 0.7;
}
.state-row--err {
  color: #f4a3a3;
}
.follow-up-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.follow-up-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: var(--space-2);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
}
.follow-up-status {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.status-chip {
  display: inline-block;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
}
.status-chip.status-queued {
  background: rgba(212, 165, 95, 0.18);
  color: #ffd9a3;
}
.status-chip.status-resolving {
  background: rgba(107, 153, 178, 0.2);
  color: #8cb4cc;
}
.due-text {
  font-style: italic;
}
.follow-up-brief {
  font-size: var(--font-sm);
  color: var(--color-text);
  line-height: 1.5;
  word-break: break-word;
}
.follow-up-msgs {
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.5;
}
.msg-preview {
  color: var(--color-text);
  opacity: 0.85;
}
</style>
