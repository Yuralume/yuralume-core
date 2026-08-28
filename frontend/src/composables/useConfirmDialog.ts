import { Modal } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

interface ConfirmDialogOptions {
  title?: string
  content?: string
  okText?: string
  cancelText?: string
  danger?: boolean
}

/**
 * 確認框的堆疊層——高於本專案所有自訂 overlay。
 *
 * ant-design-vue 的 `zIndexPopupBase` 預設是 1000，而本專案沒有用
 * `ConfigProvider` 調過它。自訂 overlay 卻普遍疊得更高：
 *
 * | overlay | z-index |
 * |---|---|
 * | `.relationship-wizard`（創角精靈） | 1200 |
 * | `StoryArcPanel` / `CharacterImagesPanel` | 1200 |
 * | `ArcTemplatePicker` | 1300 |
 * | `ArcTemplateIntakeWizard` | 1400 |
 * | `UiLightbox` | 1500 |
 *
 * 於是「確認框在某個自訂 overlay 開著的時候被喚起」＝確認框被蓋在底下：滑鼠
 * 點不到、遮罩吃掉點擊，而喚起它的 promise 永遠不 resolve。真實案例是創角
 * 精靈送出後的「同名身分卡要覆蓋嗎？」——精靈那時還開著（`visible` 要等
 * 建角＋後續工作都跑完才關）。
 *
 * 1600 高過上表全部。刻意**不**取更高的 2000：那是 `FirstLoginLocaleGate`
 * 的位置，它是「還沒選語言就什麼都別做」的閘，本來就該壓在確認框之上。
 */
export const CONFIRM_DIALOG_Z_INDEX = 1600

export function useConfirmDialog() {
  const { t } = useI18n()

  return (options: ConfirmDialogOptions): Promise<boolean> => new Promise((resolve) => {
    let settled = false
    const settle = (value: boolean) => {
      if (settled) return
      settled = true
      resolve(value)
    }

    Modal.confirm({
      class: 'app-confirm-modal',
      wrapClassName: 'app-confirm-modal',
      centered: true,
      maskClosable: true,
      zIndex: CONFIRM_DIALOG_Z_INDEX,
      title: options.title ?? t('common.actions.confirm'),
      content: options.content,
      okText: options.okText ?? t('common.actions.confirm'),
      cancelText: options.cancelText ?? t('common.actions.cancel'),
      okButtonProps: options.danger ? { danger: true } : undefined,
      onOk: () => settle(true),
      onCancel: () => settle(false),
      afterClose: () => settle(false),
    })
  })
}
