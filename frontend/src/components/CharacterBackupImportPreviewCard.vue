<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiBadge, UiButton, UiCard } from '@/components/ui'
import type { CharacterBackupImportPreview } from '@/utils/api/characterBackups'

/**
 * The confirm dialog for a staged `.lumebackup` (CB4 / CB3 §7 preview).
 *
 * Purely a display of what the manifest already says — nothing here reads
 * any data file. The one thing it insists on making impossible to miss is
 * `will_collapse_nsfw`: on a hosted import, some of what the export
 * promised will not come back verbatim, and that has to be said *before*
 * the player commits, not discovered afterward in a thinner conversation.
 */
const props = withDefaults(
  defineProps<{
    preview: CharacterBackupImportPreview
    confirmLoading?: boolean
  }>(),
  {
    confirmLoading: false,
  },
)

const emit = defineEmits<{
  confirm: []
  cancel: []
}>()

const { t } = useI18n()

const hasNsfwCollapse = computed(() => (
  props.preview.will_collapse_nsfw
  && (
    props.preview.nsfw_collapse.messages_replaced > 0
    || props.preview.nsfw_collapse.messages_dropped > 0
    || props.preview.nsfw_collapse.memories_skipped > 0
    || props.preview.nsfw_collapse.operator_profile_fields_skipped > 0
    || props.preview.nsfw_collapse.pending_follow_ups_skipped > 0
  )
))

const skippedMediaCount = computed(() => props.preview.export_report.skipped_media_keys.length)

function formatBytes(bytes: number): string {
  if (bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB'] as const
  const exponent = Math.min(
    Math.floor(Math.log(bytes) / Math.log(1024)),
    units.length - 1,
  )
  const value = bytes / 1024 ** exponent
  const precision = exponent === 0 ? 0 : 1
  return `${value.toFixed(precision)} ${units[exponent]}`
}
</script>

<template>
  <UiCard class="backup-preview" size="lg">
    <template #header>
      <h3 class="backup-preview__title">
        {{ t('characterBackup.preview.title', { name: preview.character_name }) }}
      </h3>
    </template>

    <dl class="backup-preview__stats">
      <div class="backup-preview__stat">
        <dt>{{ t('characterBackup.preview.totalRows') }}</dt>
        <dd>{{ preview.total_rows }}</dd>
      </div>
      <div class="backup-preview__stat">
        <dt>{{ t('characterBackup.preview.media') }}</dt>
        <dd>
          {{ t('characterBackup.preview.mediaCount', { count: preview.media_count }) }}
          <span class="backup-preview__stat-sub">
            ({{ formatBytes(preview.media_total_bytes) }})
          </span>
        </dd>
      </div>
      <div v-if="preview.exported_at" class="backup-preview__stat">
        <dt>{{ t('characterBackup.preview.exportedAt') }}</dt>
        <dd>{{ preview.exported_at }}</dd>
      </div>
    </dl>

    <p v-if="skippedMediaCount > 0" class="backup-preview__note">
      {{ t('characterBackup.preview.skippedMediaAtExport', { count: skippedMediaCount }) }}
    </p>

    <div v-if="hasNsfwCollapse" class="backup-preview__nsfw" role="note">
      <UiBadge variant="warning">{{ t('characterBackup.preview.nsfwBadge') }}</UiBadge>
      <p class="backup-preview__nsfw-lead">
        {{ t('characterBackup.preview.nsfwLead') }}
      </p>
      <ul class="backup-preview__nsfw-list">
        <li v-if="preview.nsfw_collapse.messages_replaced > 0">
          {{ t('characterBackup.preview.nsfwReplaced', {
            count: preview.nsfw_collapse.messages_replaced,
          }) }}
        </li>
        <li v-if="preview.nsfw_collapse.messages_dropped > 0">
          {{ t('characterBackup.preview.nsfwDropped', {
            count: preview.nsfw_collapse.messages_dropped,
          }) }}
        </li>
        <li v-if="preview.nsfw_collapse.memories_skipped > 0">
          {{ t('characterBackup.preview.nsfwMemoriesSkipped', {
            count: preview.nsfw_collapse.memories_skipped,
          }) }}
        </li>
        <li v-if="preview.nsfw_collapse.operator_profile_fields_skipped > 0">
          {{ t('characterBackup.preview.nsfwProfileFieldsSkipped', {
            count: preview.nsfw_collapse.operator_profile_fields_skipped,
          }) }}
        </li>
        <li v-if="preview.nsfw_collapse.pending_follow_ups_skipped > 0">
          {{ t('characterBackup.preview.nsfwPendingFollowUpsSkipped', {
            count: preview.nsfw_collapse.pending_follow_ups_skipped,
          }) }}
        </li>
      </ul>
    </div>

    <template #footer>
      <div class="backup-preview__actions">
        <UiButton
          variant="secondary"
          size="sm"
          :disabled="confirmLoading"
          @click="emit('cancel')"
        >
          {{ t('common.actions.cancel') }}
        </UiButton>
        <UiButton
          variant="primary"
          size="sm"
          :loading="confirmLoading"
          @click="emit('confirm')"
        >
          {{ t('characterBackup.preview.confirmAction') }}
        </UiButton>
      </div>
    </template>
  </UiCard>
</template>

<style scoped>
.backup-preview__title {
  margin: 0;
  font-size: var(--font-md);
  font-weight: 650;
}

.backup-preview__stats {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin: 0;
}

.backup-preview__stat {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
  font-size: var(--font-sm);
}

.backup-preview__stat dt {
  color: var(--color-text-secondary);
}

.backup-preview__stat dd {
  margin: 0;
  color: var(--color-text);
  font-weight: 600;
  text-align: right;
}

.backup-preview__stat-sub {
  color: var(--color-text-secondary);
  font-weight: 400;
}

.backup-preview__note {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}

.backup-preview__nsfw {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: var(--space-1);
  padding: var(--space-3);
  border: 1px solid rgba(255, 209, 128, 0.4);
  border-radius: 6px;
  background: rgba(255, 209, 128, 0.1);
}

.backup-preview__nsfw-lead {
  margin: 0;
  color: #ffd180;
  font-size: var(--font-sm);
  font-weight: 600;
  line-height: 1.5;
}

.backup-preview__nsfw-list {
  margin: 0;
  padding-left: 18px;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}

.backup-preview__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
}
</style>
