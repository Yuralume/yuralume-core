<script setup lang="ts">
import { computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { UiButton } from '@/components/ui'
import CharacterBackupPasswordModal from '@/components/CharacterBackupPasswordModal.vue'
import { useCharacterBackupExport } from '@/composables/useCharacterBackupExport'
import { CHARACTER_BACKUP_EXPORT_STAGES } from '@/utils/api/characterBackups'
import type { Character } from '@/types/character'

/**
 * The player-facing (and admin-reused) backup export panel (CB4).
 *
 * Restore lives in `CharacterBackupRestorePanel` instead: restoring a
 * `.lumebackup` always creates a brand-new character (D4) and never touches
 * `character`, so it is mounted next to the "add a character" surfaces
 * instead — those stay reachable with zero characters, unlike this panel's
 * character-scoped mount points. Callers remount this component on
 * character change (`:key="character.id"`) rather than this panel watching
 * for it itself, the same way `CharacterEditPanel` is keyed at its mount
 * sites.
 */
const props = defineProps<{
  character: Character
}>()

const { t } = useI18n()

const {
  passwordModalOpen: exportPasswordModalOpen,
  submitting: exportSubmitting,
  polling: exportPolling,
  checkingDownload: exportCheckingDownload,
  job: exportJob,
  errorKey: exportErrorKey,
  errorDetail: exportErrorDetail,
  openPasswordModal: openExportPasswordModal,
  closePasswordModal: closeExportPasswordModal,
  submit: submitExport,
  download: downloadExport,
  reset: resetExport,
} = useCharacterBackupExport()

const exportJobRunning = computed(() => exportSubmitting.value || exportPolling.value)
const exportSucceeded = computed(() => exportJob.value?.status === 'succeeded')
const exportFailed = computed(() => exportJob.value?.status === 'failed')

const exportStagePercent = computed(() => (
  exportJob.value ? stagePercent(exportJob.value.stage, CHARACTER_BACKUP_EXPORT_STAGES) : 0
))
const exportStageLabel = computed(() => (
  exportJob.value ? stageLabel(exportJob.value.stage) : ''
))

/** Persistent home for a terminal `failed` export job (FE-2) — the toast
 * below fires once, but the panel itself must keep saying so for as long
 * as the failed job stays on screen. */
const exportFailureMessage = computed(() => (
  exportFailed.value && exportErrorKey.value
    ? t(`characterBackup.errors.${exportErrorKey.value}`)
    : null
))

function stagePercent(stage: string, stages: readonly string[]): number {
  const index = stages.indexOf(stage)
  if (index < 0) return 0
  return Math.round(((index + 1) / stages.length) * 100)
}

function stageLabel(stage: string): string {
  const fallback = t('characterBackup.stages.export.queued')
  if (!stage || stage === 'failed') return fallback
  return t(`characterBackup.stages.export.${stage}`, fallback)
}

async function submitExportPassword(password: string) {
  await submitExport(props.character.id, password)
  if (exportJob.value?.status === 'succeeded') {
    notification.success({
      message: t('characterBackup.export.readyNotice', { name: props.character.name }),
    })
  }
}

function startNewExport() {
  resetExport()
  openExportPasswordModal()
}

// 409/429/… on export start have no persistent home on screen (the modal
// that triggered them is already closed) — CB4's UX note calls for a toast
// here specifically.
watch(exportErrorKey, (key) => {
  if (!key) return
  notification.error({
    message: t(`characterBackup.errors.${key}`),
    description: exportErrorDetail.value ?? undefined,
  })
})
</script>

<template>
  <div class="character-backup">
    <!-- 匯出 -->
    <section class="character-backup__section">
      <h4 class="character-backup__section-title">{{ t('characterBackup.export.title') }}</h4>
      <p class="character-backup__hint">{{ t('characterBackup.export.hint') }}</p>

      <div class="character-backup__actions">
        <UiButton
          variant="primary"
          size="sm"
          :loading="exportJobRunning"
          :disabled="exportJobRunning"
          @click="openExportPasswordModal()"
        >
          {{ t('characterBackup.export.startAction') }}
        </UiButton>
        <UiButton
          v-if="exportSucceeded || exportFailed"
          variant="secondary"
          size="sm"
          :disabled="exportJobRunning"
          @click="startNewExport"
        >
          {{ t('characterBackup.export.startOverAction') }}
        </UiButton>
      </div>

      <p v-if="exportFailureMessage" class="character-backup__error" role="alert">
        {{ exportFailureMessage }}
      </p>

      <div v-if="exportJob && exportJobRunning" class="character-backup__progress">
        <div class="character-backup__progress-head">
          <span>{{ exportStageLabel }}</span>
          <span>{{ exportStagePercent }}%</span>
        </div>
        <div class="character-backup__progress-bar">
          <div class="character-backup__progress-fill" :style="{ width: `${exportStagePercent}%` }" />
        </div>
      </div>

      <div v-if="exportSucceeded" class="character-backup__download">
        <p class="character-backup__note">{{ t('characterBackup.export.readyHint') }}</p>
        <UiButton
          variant="secondary"
          size="sm"
          :loading="exportCheckingDownload"
          @click="downloadExport(character.id)"
        >
          {{ t('characterBackup.export.downloadAction') }}
        </UiButton>
      </div>
    </section>

    <CharacterBackupPasswordModal
      :visible="exportPasswordModalOpen"
      mode="export"
      :loading="exportSubmitting"
      @close="closeExportPasswordModal()"
      @confirm="submitExportPassword"
    />
  </div>
</template>

<style scoped>
.character-backup {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.character-backup__section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.character-backup__section-title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-sm);
  font-weight: 650;
}

.character-backup__hint,
.character-backup__note {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}

.character-backup__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.character-backup__download {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  align-items: flex-start;
}

.character-backup__progress {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 10px 12px;
  border: 1px solid rgba(var(--color-primary-rgb), 0.28);
  border-radius: 6px;
  background: rgba(var(--color-primary-rgb), 0.08);
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}

.character-backup__progress-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}

.character-backup__progress-bar {
  height: 6px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.08);
  overflow: hidden;
}

.character-backup__progress-fill {
  height: 100%;
  background: var(--color-primary-light);
  transition: width 0.3s ease;
}

.character-backup__error {
  margin: 0;
  color: #f4a3a3;
  font-size: var(--font-xs);
  line-height: 1.6;
}
</style>
