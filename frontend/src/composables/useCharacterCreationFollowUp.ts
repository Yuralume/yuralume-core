import { h } from 'vue'
import { useI18n } from 'vue-i18n'
import { notification } from 'ant-design-vue'
import { UiButton } from '@/components/ui'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import {
  createIdentityCard,
  identityCardErrorDetail,
  isIdentityCardLimitReached,
  IDENTITY_CARDS_PER_OPERATOR,
} from '@/utils/api/identityCards'
import { updatePlayerPersonaNote } from '@/utils/api/playerPersonaNote'
import {
  hasCharacterCreationFollowUpWork,
  runCharacterCreationFollowUp,
  type CharacterCreationFollowUp,
  type CharacterCreationFollowUpDeps,
  type CharacterCreationFollowUpOutcome,
} from '@/utils/characterCreationFollowUp'

/**
 * 建角成功之後的兩件附帶工作，接上真正的 HTTP 與玩家提示（IC2）。
 *
 * 三個掛精靈的入口（玩家角色卡面板、admin 匯入、admin 市集）共用這一顆：
 * 提示文案、覆蓋確認、重試入口因此只有一份，不會有哪個入口悄悄少一半。
 *
 * 失敗的處理方式刻意只有一種——**不回滾角色**，改成一則不會自己消失的錯誤
 * 提示加一顆重試按鈕。重試只重跑失敗的那一步：note 寫成功、存卡失敗時再按
 * 一次不會把 note 重寫一遍。
 */
export function useCharacterCreationFollowUp() {
  const { t } = useI18n()
  const confirmDialog = useConfirmDialog()

  const deps: CharacterCreationFollowUpDeps = {
    writeNote: (characterId, note) => updatePlayerPersonaNote(characterId, note),
    createCard: body => createIdentityCard(body),
    confirmOverwrite: name => confirmDialog({
      title: t('identityCard.overwrite.title'),
      content: t('identityCard.overwrite.content', { name }),
      okText: t('identityCard.overwrite.ok'),
    }),
  }

  async function run(
    characterId: string,
    followUp: CharacterCreationFollowUp,
  ): Promise<void> {
    // 沒有要做的事就一個請求都不發——「完全不碰人設欄與存卡勾選」的建角
    // 路徑必須與加這個功能之前逐字等價。
    if (!hasCharacterCreationFollowUpWork(followUp)) return

    const outcome = await runCharacterCreationFollowUp(characterId, followUp, deps)

    if (outcome.card === 'done') {
      notification.success({ message: t('identityCard.followUp.cardSaved') })
    }

    if (outcome.note !== 'failed' && outcome.card !== 'failed') return

    // 失敗一定要說，但不是每種失敗都值得給重試鍵——卡片存滿了再按幾次都是
    // 同一個 409，玩家要做的是先去刪一張卡。
    const pending = remainingWork(followUp, outcome)
    const retryable = hasCharacterCreationFollowUpWork(pending)
    const key = `character-creation-follow-up-${characterId}`
    notification.error({
      key,
      // 角色已經建好了，這則提示不該自己滑掉——它是唯一的重試入口。
      duration: 0,
      message: failureMessage(outcome),
      description: failureDescription(outcome),
      btn: retryable
        ? () => h(
          UiButton,
          {
            variant: 'primary',
            size: 'sm',
            onClick: () => {
              notification.close(key)
              void run(characterId, pending)
            },
          },
          () => t('common.actions.retry'),
        )
        : undefined,
    })
  }

  function failureMessage(outcome: CharacterCreationFollowUpOutcome): string {
    if (outcome.note === 'failed' && outcome.card === 'failed') {
      return t('identityCard.followUp.bothFailed')
    }
    return outcome.note === 'failed'
      ? t('identityCard.followUp.personaNoteFailed')
      : t('identityCard.followUp.cardSaveFailed')
  }

  function failureDescription(outcome: CharacterCreationFollowUpOutcome): string {
    // 上限是自己一種狀況：重試不會有幫助，玩家要先去刪一張卡。
    if (isIdentityCardLimitReached(outcome.cardError)) {
      const detail = identityCardErrorDetail(outcome.cardError)
      const limit = typeof detail?.limit === 'number' ? detail.limit : IDENTITY_CARDS_PER_OPERATOR
      return t('identityCard.followUp.limitReached', { limit })
    }
    const detail = errorText(outcome.noteError) || errorText(outcome.cardError)
    const hint = t('identityCard.followUp.characterStillCreated')
    return detail ? `${hint}（${detail}）` : hint
  }

  return { run }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : ''
}

/**
 * 只留下「還沒做成功、而且重試有意義」的那幾步。
 *
 * `declined`（玩家在覆蓋確認裡按了取消）不算待辦——那是明確的決定，不該被
 * 一顆重試按鈕拉回來。上限已滿同理：重試只會拿到同一個 409。
 */
function remainingWork(
  followUp: CharacterCreationFollowUp,
  outcome: CharacterCreationFollowUpOutcome,
): CharacterCreationFollowUp {
  const cardRetryable = outcome.card === 'failed'
    && !isIdentityCardLimitReached(outcome.cardError)
  return {
    personaNote: outcome.note === 'failed' ? followUp.personaNote : '',
    saveCardName: cardRetryable ? followUp.saveCardName : null,
    cardContent: followUp.cardContent,
  }
}
