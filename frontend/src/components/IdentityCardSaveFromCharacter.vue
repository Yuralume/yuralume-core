<script setup lang="ts">
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import { UiButton, UiInput } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import { getInitialRelationship } from '@/utils/api/initialRelationship'
import { getPlayerPersonaNote } from '@/utils/api/playerPersonaNote'
import {
  createIdentityCard,
  identityCardErrorDetail,
  isIdentityCardLimitReached,
  IDENTITY_CARD_NAME_MAX_CHARS,
  IDENTITY_CARDS_PER_OPERATOR,
} from '@/utils/api/identityCards'
import {
  saveIdentityCardFromCharacter,
  type SaveIdentityCardFromCharacterDeps,
} from '@/utils/identityCardSaveFromCharacter'

/**
 * 設定頁「從既有角色回存」入口（IC3）——放在「關係與相處設定」與「你的
 * 人設」兩個 CollapsibleSection 共同的層級（`CharacterSettingsSection.vue`
 * 本身），不是塞進任一邊裡面：這裡存的是這兩塊**合起來**的整組設定。
 *
 * 用的是已儲存的值：按下「存成身分卡」的當下才重新 GET 一次關係 seed 與
 * 玩家人設，不是讀正在編輯中的表單草稿——語意是「把這個角色現在的設定存
 * 成卡」，跟頁面上另外兩個編輯器的儲存狀態無關（也不必等它們先存檔）。
 *
 * 真正的順序（讀值 → 建內容 → 撞名才問覆蓋）在 `identityCardSaveFromCharacter.ts`
 * 測過；這裡只做 HTTP 與 i18n 接線。
 */
const props = defineProps<{
  character: Character
}>()

const { t } = useI18n()
const confirmDialog = useConfirmDialog()

const open = ref(false)
const name = ref('')
const saving = ref(false)
const error = ref<string | null>(null)
const savedFeedback = ref(false)

const nameMaxChars = IDENTITY_CARD_NAME_MAX_CHARS
// 比照創角精靈（InitialRelationshipWizardModal.vue 的 canSubmit）：名稱空白
// 或超過上限都不准送出，不要等後端 422 才發現。
const nameInvalid = computed(() => {
  const trimmed = name.value.trim()
  return !trimmed || trimmed.length > nameMaxChars
})

function startSave() {
  open.value = true
  savedFeedback.value = false
  error.value = null
  name.value = props.character.name
}

function cancel() {
  if (saving.value) return
  open.value = false
  error.value = null
}

const deps: SaveIdentityCardFromCharacterDeps = {
  loadSeed: characterId => getInitialRelationship(characterId),
  loadPersonaNote: characterId => getPlayerPersonaNote(characterId),
  createCard: body => createIdentityCard(body),
  confirmOverwrite: cardName => confirmDialog({
    title: t('identityCard.overwrite.title'),
    content: t('identityCard.overwrite.content', { name: cardName }),
    okText: t('identityCard.overwrite.ok'),
  }),
}

async function save() {
  if (saving.value) return
  const trimmed = name.value.trim()
  if (!trimmed || trimmed.length > nameMaxChars) return

  saving.value = true
  error.value = null
  try {
    const outcome = await saveIdentityCardFromCharacter(props.character.id, trimmed, deps)
    if (outcome.status === 'done') {
      open.value = false
      savedFeedback.value = true
      return
    }
    if (outcome.status === 'declined') {
      // 玩家在覆蓋確認裡按了取消——維持在編輯狀態，不當成錯誤。
      return
    }
    if (outcome.status === 'load_failed') {
      error.value = t('identityCard.saveFromCharacter.loadFailed')
      return
    }
    if (outcome.status === 'empty') {
      error.value = t('identityCard.saveFromCharacter.empty')
      return
    }
    error.value = isIdentityCardLimitReached(outcome.error)
      ? t('identityCard.followUp.limitReached', {
        limit: identityCardLimit(outcome.error),
      })
      : t('identityCard.followUp.cardSaveFailed')
  } finally {
    saving.value = false
  }
}

function identityCardLimit(error: unknown): number {
  const detail = identityCardErrorDetail(error)
  return typeof detail?.limit === 'number' ? detail.limit : IDENTITY_CARDS_PER_OPERATOR
}
</script>

<template>
  <section class="identity-card-save-entry">
    <template v-if="!open">
      <UiButton variant="secondary" size="sm" @click="startSave">
        {{ t('identityCard.saveFromCharacter.action') }}
      </UiButton>
      <p v-if="savedFeedback" class="identity-card-save-entry__feedback">
        {{ t('identityCard.followUp.cardSaved') }}
      </p>
    </template>

    <template v-else>
      <p class="identity-card-save-entry__hint">
        {{ t('identityCard.saveFromCharacter.hint', { name: character.name }) }}
      </p>
      <UiInput
        v-model="name"
        :label="t('identityCard.save.nameLabel')"
        :placeholder="t('identityCard.save.namePlaceholder')"
        :disabled="saving"
        required
      />
      <p class="identity-card-save-entry__hint">
        {{ t('identityCard.save.hint', { max: nameMaxChars }) }}
      </p>

      <p v-if="error" class="identity-card-save-entry__error" role="alert">
        {{ error }}
      </p>

      <div class="identity-card-save-entry__actions">
        <UiButton variant="ghost" size="sm" :disabled="saving" @click="cancel">
          {{ t('common.actions.cancel') }}
        </UiButton>
        <UiButton
          variant="primary"
          size="sm"
          :loading="saving"
          :disabled="nameInvalid"
          @click="save"
        >
          {{ t('common.actions.save') }}
        </UiButton>
      </div>
    </template>
  </section>
</template>

<style scoped>
.identity-card-save-entry {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}

.identity-card-save-entry__hint,
.identity-card-save-entry__feedback {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.identity-card-save-entry__feedback {
  color: #7dc49a;
}

.identity-card-save-entry__error {
  margin: 0;
  color: #f4a3a3;
  font-size: 11px;
  line-height: 1.45;
}

.identity-card-save-entry__actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
</style>
