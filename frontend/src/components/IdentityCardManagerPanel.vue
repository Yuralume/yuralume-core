<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { UiButton, UiInput } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { usePlayerIdentityCards } from '@/composables/usePlayerIdentityCards'
import { useTimezone } from '@/composables/useTimezone'
import { formatDateTime } from '@/i18n/formatters'
import {
  isIdentityCardNameConflict,
  IDENTITY_CARD_NAME_MAX_CHARS,
  type IdentityCard,
} from '@/utils/api/identityCards'
import IdentityCardPreviewDialog from './IdentityCardPreviewDialog.vue'

/**
 * 設定頁「玩家身分卡」管理面（IC3）——列表、改名、刪除、預覽。**第一版
 * 沒有內容編輯**，欄位值只能透過創角精靈或「從既有角色回存」的入口整份
 * 覆蓋。刪卡不影響已經用那張卡建立的角色（快照語意，卡片表上零 character
 * 參照欄——見 IC1 的 migration）。
 *
 * 清單載入／改名／刪除的 HTTP 與本地狀態在 `usePlayerIdentityCards`；這裡
 * 只加 i18n 提示與刪除前的確認對話框。
 */
const { t, locale } = useI18n()
const { timeZone } = useTimezone()
const confirmDialog = useConfirmDialog()
const { cards, limit, loading, loaded, load, rename, remove } = usePlayerIdentityCards()

const renamingId = ref<string | null>(null)
const renameDraft = ref('')
const renameSaving = ref(false)
const renameError = ref<string | null>(null)

const nameMaxChars = IDENTITY_CARD_NAME_MAX_CHARS
// 比照回存入口（IdentityCardSaveFromCharacter.vue）：空白或超過上限都不准
// 送出，不要讓玩家在這裡按下去才落到後端通用的「改名失敗」。
const renameNameInvalid = computed(() => {
  const trimmed = renameDraft.value.trim()
  return !trimmed || trimmed.length > nameMaxChars
})

const previewCard = ref<IdentityCard | null>(null)

const feedback = ref<string | null>(null)
const feedbackIsError = ref(false)

/** 讀不到現況：既不是「載入中」也不是「還沒填」，不能拿其中任一個充數。 */
const loadFailed = computed(() => !loading.value && !loaded.value)
const atLimit = computed(() => loaded.value && cards.value.length >= limit.value)
const countLabel = computed(() => t('identityCard.manage.count', {
  count: cards.value.length,
  limit: limit.value,
}))

onMounted(() => {
  void load()
})

function updatedAtText(card: IdentityCard): string {
  return t('identityCard.manage.updatedAtLabel', {
    time: formatDateTime(card.updated_at, locale.value, timeZone.value),
  })
}

function startRename(card: IdentityCard) {
  renamingId.value = card.id
  renameDraft.value = card.name
  renameError.value = null
}

function cancelRename() {
  if (renameSaving.value) return
  renamingId.value = null
  renameError.value = null
}

async function confirmRename(card: IdentityCard) {
  if (renameSaving.value) return
  const name = renameDraft.value.trim()
  if (!name || name.length > nameMaxChars) return

  renameSaving.value = true
  renameError.value = null
  try {
    await rename(card.id, name)
    renamingId.value = null
    feedback.value = t('identityCard.manage.rename.saved')
    feedbackIsError.value = false
  } catch (err) {
    renameError.value = isIdentityCardNameConflict(err)
      ? t('identityCard.manage.rename.conflict')
      : t('identityCard.manage.rename.failed')
  } finally {
    renameSaving.value = false
  }
}

async function confirmDeleteCard(card: IdentityCard) {
  const confirmed = await confirmDialog({
    title: t('identityCard.manage.delete.confirmTitle'),
    content: t('identityCard.manage.delete.confirmContent', { name: card.name }),
    okText: t('common.actions.delete'),
    danger: true,
  })
  if (!confirmed) return

  try {
    await remove(card.id)
    if (previewCard.value?.id === card.id) previewCard.value = null
    feedback.value = t('identityCard.manage.delete.done')
    feedbackIsError.value = false
  } catch {
    feedback.value = t('identityCard.manage.delete.failed')
    feedbackIsError.value = true
  }
}

function openPreview(card: IdentityCard) {
  previewCard.value = card
}
</script>

<template>
  <div class="identity-card-manager">
    <p v-if="loading" class="identity-card-manager__hint">
      {{ t('identityCard.manage.loading') }}
    </p>
    <div v-else-if="loadFailed" class="identity-card-manager__load-failed">
      <p role="alert">{{ t('identityCard.manage.loadFailed') }}</p>
      <UiButton variant="secondary" size="sm" @click="load">
        {{ t('common.actions.retry') }}
      </UiButton>
    </div>

    <template v-else>
      <p v-if="cards.length === 0" class="identity-card-manager__hint">
        {{ t('identityCard.manage.empty') }}
      </p>

      <template v-else>
        <p class="identity-card-manager__count">
          {{ countLabel }}
          <span v-if="atLimit" class="identity-card-manager__limit-note">
            {{ t('identityCard.manage.limitNote') }}
          </span>
        </p>

        <ul class="identity-card-manager__list">
          <li
            v-for="card in cards"
            :key="card.id"
            class="identity-card-manager__row"
          >
            <template v-if="renamingId === card.id">
              <UiInput
                v-model="renameDraft"
                :placeholder="t('identityCard.manage.rename.namePlaceholder')"
                :disabled="renameSaving"
              />
              <p class="identity-card-manager__hint">
                {{ t('identityCard.manage.rename.hint', { max: nameMaxChars }) }}
              </p>
              <p v-if="renameError" class="identity-card-manager__error" role="alert">
                {{ renameError }}
              </p>
              <div class="identity-card-manager__row-actions">
                <UiButton
                  variant="ghost"
                  size="sm"
                  :disabled="renameSaving"
                  @click="cancelRename"
                >
                  {{ t('common.actions.cancel') }}
                </UiButton>
                <UiButton
                  variant="primary"
                  size="sm"
                  :loading="renameSaving"
                  :disabled="renameNameInvalid"
                  @click="confirmRename(card)"
                >
                  {{ t('common.actions.save') }}
                </UiButton>
              </div>
            </template>

            <template v-else>
              <div class="identity-card-manager__row-main">
                <span class="identity-card-manager__name">{{ card.name }}</span>
                <span class="identity-card-manager__updated">{{ updatedAtText(card) }}</span>
              </div>
              <div class="identity-card-manager__row-actions">
                <UiButton variant="ghost" size="sm" @click="openPreview(card)">
                  {{ t('identityCard.manage.previewAction') }}
                </UiButton>
                <UiButton variant="ghost" size="sm" @click="startRename(card)">
                  {{ t('identityCard.manage.renameAction') }}
                </UiButton>
                <UiButton variant="danger" size="sm" @click="confirmDeleteCard(card)">
                  {{ t('common.actions.delete') }}
                </UiButton>
              </div>
            </template>
          </li>
        </ul>
      </template>
    </template>

    <p
      v-if="feedback"
      class="identity-card-manager__feedback"
      :class="{ 'identity-card-manager__feedback--error': feedbackIsError }"
    >
      {{ feedback }}
    </p>

    <IdentityCardPreviewDialog :card="previewCard" @close="previewCard = null" />
  </div>
</template>

<style scoped>
.identity-card-manager {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.identity-card-manager__hint,
.identity-card-manager__feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.identity-card-manager__feedback {
  color: #7dc49a;
}

.identity-card-manager__feedback--error {
  color: #f4a3a3;
}

.identity-card-manager__load-failed {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
  color: #f4a3a3;
  font-size: 11px;
}

.identity-card-manager__count {
  margin: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  color: var(--color-text-secondary);
  font-size: 11px;
}

.identity-card-manager__limit-note {
  color: #f0a868;
}

.identity-card-manager__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.identity-card-manager__row {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.02);
}

.identity-card-manager__row-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.identity-card-manager__name {
  color: var(--color-text);
  font-size: var(--font-sm);
  font-weight: 600;
}

.identity-card-manager__updated {
  color: var(--color-text-secondary);
  font-size: 11px;
}

.identity-card-manager__row-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.identity-card-manager__error {
  margin: 0;
  color: #f4a3a3;
  font-size: 11px;
  line-height: 1.45;
}
</style>
