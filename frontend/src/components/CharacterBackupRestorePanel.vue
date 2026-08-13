<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { UiButton } from '@/components/ui'
import CharacterBackupPasswordModal from '@/components/CharacterBackupPasswordModal.vue'
import CharacterBackupImportPreviewCard from '@/components/CharacterBackupImportPreviewCard.vue'
import { useCharacterBackupImport } from '@/composables/useCharacterBackupImport'
import { CHARACTER_BACKUP_RESTORE_STAGES } from '@/utils/api/characterBackups'
import { getCharacter } from '@/utils/api/characters'
import type { Character } from '@/types/character'

/**
 * The character-agnostic half of the backup/restore feature (CB4 follow-up):
 * restoring a `.lumebackup` always creates a brand-new character (D4), so
 * unlike `CharacterBackupPanel`'s export side it never needs an existing
 * character to hang off of. Mounted next to the "add a character" surfaces
 * (character-card panel, admin create card) so it stays reachable with zero
 * characters — `CharacterBackupPanel` lives inside character-scoped
 * settings, which is structurally unreachable at that point.
 */
const emit = defineEmits<{
  imported: [character: Character]
}>()

const { t } = useI18n()

const {
  phase,
  passwordModalOpen,
  fileName,
  preview,
  job,
  errorKey,
  errorDetail,
  selectFile,
  submitPassword,
  confirm,
  closePasswordModal,
  reset,
} = useCharacterBackupImport()

const fileInputRef = ref<HTMLInputElement | null>(null)

const stagePercent = computed(() => {
  const stage = job.value?.stage
  if (!stage) return 0
  const index = CHARACTER_BACKUP_RESTORE_STAGES.indexOf(
    stage as (typeof CHARACTER_BACKUP_RESTORE_STAGES)[number],
  )
  if (index < 0) return 0
  return Math.round(((index + 1) / CHARACTER_BACKUP_RESTORE_STAGES.length) * 100)
})

const stageLabel = computed(() => {
  const fallback = t('characterBackup.stages.restore.queued')
  const stage = job.value?.stage
  if (!stage || stage === 'failed') return fallback
  return t(`characterBackup.stages.restore.${stage}`, fallback)
})

/** Only the wrong-password refusal has a home inside the password modal —
 * every other refusal closes the modal and reaches the player as a toast
 * (see the watcher below). */
const passwordModalError = computed(() => (
  errorKey.value === 'wrongPassword' ? t('characterBackup.errors.wrongPassword') : null
))

const failureMessage = computed(() => (
  phase.value === 'failed' && errorKey.value
    ? t(`characterBackup.errors.${errorKey.value}`)
    : null
))

function triggerFilePicker() {
  fileInputRef.value?.click()
}

function handleFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  void selectFile(file)
}

// 409/429/… on start, and every non-password refusal, have no persistent
// home on screen (the modal that triggered them is already closed) — same
// toast fallback `CharacterBackupPanel` uses.
watch(errorKey, (key) => {
  if (!key || key === 'wrongPassword') return
  notification.error({
    message: t(`characterBackup.errors.${key}`),
    description: errorDetail.value ?? undefined,
  })
})

// A restore only ever produces a character id — fetch the full record once
// so the caller can slot it into the same "push + select" chain a card
// install already uses, rather than teaching every caller to fetch by id.
watch(phase, async (nextPhase) => {
  if (nextPhase !== 'succeeded') return
  const newCharacterId = job.value?.character_id
  if (!newCharacterId) return
  try {
    const character = await getCharacter(newCharacterId)
    notification.success({
      message: t('characterBackup.import.doneNotice', { name: character.name }),
    })
    emit('imported', character)
  } catch {
    // The restore itself already succeeded and its report is on screen;
    // failing to re-fetch the fresh record just means the roster elsewhere
    // in the app catches up on its next normal reload instead of this one.
  }
})
</script>

<template>
  <section class="backup-restore">
    <h4 class="backup-restore__title">{{ t('characterBackup.import.title') }}</h4>
    <p class="backup-restore__hint">{{ t('characterBackup.import.hint') }}</p>

    <template v-if="phase === 'idle' || phase === 'failed'">
      <div class="backup-restore__actions">
        <UiButton variant="secondary" size="sm" @click="triggerFilePicker">
          {{ t('characterBackup.import.selectFileAction') }}
        </UiButton>
      </div>
      <p v-if="failureMessage" class="backup-restore__error" role="alert">
        {{ failureMessage }}
      </p>
    </template>

    <div
      v-else-if="phase === 'uploading'"
      class="backup-restore__progress"
    >
      <span>{{ t('characterBackup.import.uploading', { name: fileName }) }}</span>
    </div>

    <CharacterBackupImportPreviewCard
      v-else-if="phase === 'previewReady' && preview"
      :preview="preview"
      @confirm="confirm()"
      @cancel="reset()"
    />

    <div
      v-else-if="phase === 'confirming' || phase === 'restoring'"
      class="backup-restore__progress"
    >
      <div class="backup-restore__progress-head">
        <span>{{ stageLabel }}</span>
        <span>{{ stagePercent }}%</span>
      </div>
      <div class="backup-restore__progress-bar">
        <div class="backup-restore__progress-fill" :style="{ width: `${stagePercent}%` }" />
      </div>
    </div>

    <div v-else-if="phase === 'succeeded'" class="backup-restore__result">
      <p class="backup-restore__note">{{ t('characterBackup.import.summaryReplaced', {
        count: job?.report?.nsfw_messages_replaced ?? 0,
      }) }}</p>
      <p class="backup-restore__note">{{ t('characterBackup.import.summaryDropped', {
        count: job?.report?.nsfw_messages_dropped ?? 0,
      }) }}</p>
      <p class="backup-restore__note">{{ t('characterBackup.import.summaryMediaTransferred', {
        count: job?.report?.media_transferred ?? 0,
      }) }}</p>
      <ul
        v-if="(job?.report?.media_missing.length ?? 0) > 0"
        class="backup-restore__missing-media"
      >
        <li class="backup-restore__missing-media-lead">
          {{ t('characterBackup.import.mediaMissingLead', {
            count: job?.report?.media_missing.length ?? 0,
          }) }}
        </li>
        <li
          v-for="key in (job?.report?.media_missing ?? []).slice(0, 20)"
          :key="key"
          class="backup-restore__missing-media-item"
        >{{ key }}</li>
      </ul>
      <p
        v-if="job?.report?.embedding.status === 'pending'"
        class="backup-restore__note"
      >
        {{ t('characterBackup.import.embeddingPending') }}
      </p>
      <p
        v-else-if="job?.report?.embedding.status === 'failed'"
        class="backup-restore__note"
      >
        {{ t('characterBackup.import.embeddingFailed') }}
      </p>
      <UiButton variant="secondary" size="sm" @click="reset()">
        {{ t('characterBackup.import.importAnotherAction') }}
      </UiButton>
    </div>

    <input
      ref="fileInputRef"
      type="file"
      accept=".lumebackup"
      class="backup-restore__file"
      @change="handleFileChange"
    />

    <CharacterBackupPasswordModal
      :visible="passwordModalOpen"
      mode="import"
      :loading="phase === 'previewing'"
      :error-message="passwordModalError"
      @close="closePasswordModal()"
      @confirm="submitPassword($event)"
    />
  </section>
</template>

<style scoped>
.backup-restore {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.backup-restore__title {
  margin: 0;
  color: var(--color-text);
  font-size: var(--font-sm);
  font-weight: 650;
}

.backup-restore__hint,
.backup-restore__note {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}

.backup-restore__actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}

.backup-restore__progress {
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

.backup-restore__progress-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
}

.backup-restore__progress-bar {
  height: 6px;
  border-radius: 3px;
  background: rgba(255, 255, 255, 0.08);
  overflow: hidden;
}

.backup-restore__progress-fill {
  height: 100%;
  background: var(--color-primary-light);
  transition: width 0.3s ease;
}

.backup-restore__error {
  margin: 0;
  color: #f4a3a3;
  font-size: var(--font-xs);
  line-height: 1.6;
}

.backup-restore__result {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  align-items: flex-start;
}

.backup-restore__missing-media {
  margin: 0;
  padding-left: 18px;
  max-height: 140px;
  overflow-y: auto;
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.6;
}

.backup-restore__missing-media-lead {
  list-style: none;
  margin-left: -18px;
  font-weight: 600;
}

.backup-restore__missing-media-item {
  word-break: break-all;
}

.backup-restore__file {
  display: none;
}
</style>
