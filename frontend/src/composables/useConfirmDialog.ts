import { Modal } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'

interface ConfirmDialogOptions {
  title?: string
  content?: string
  okText?: string
  cancelText?: string
  danger?: boolean
}

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
