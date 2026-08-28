/**
 * 共用確認框的堆疊層（IC 審查修復 FIX-B/1）。
 *
 * 這個 repo 沒有 jsdom / @vue/test-utils，量不到「像素上誰蓋住誰」。但這個
 * bug 本來就不是渲染問題而是**數字比大小**：ant-design-vue 的 modal 預設
 * 1000，本專案的自訂 overlay 卻疊到 1500，於是在 overlay 上喚起的確認框被
 * 蓋在底下——玩家點不到，而等它的 promise 永遠不 resolve（最典型是創角精靈
 * 送出後的「同名身分卡要覆蓋嗎？」，精靈那時還開著）。
 *
 * 所以這裡直接釘那個比較：確認框確實帶著 zIndex 送進 Modal.confirm，而且那
 * 個值高過每一個會在其上喚起確認框的自訂 overlay。日後誰加了一層更高的
 * overlay，這裡會紅。
 */

import { describe, expect, it, vi } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const confirmSpy = vi.hoisted(() => vi.fn())

// 真品要 document；這裡只在意送進去的選項。
vi.mock('ant-design-vue', () => ({ Modal: { confirm: confirmSpy } }))
// useConfirmDialog 在 setup 之外被呼叫時 useI18n 會炸，文案不是本測試的主題。
vi.mock('vue-i18n', () => ({ useI18n: () => ({ t: (key: string) => key }) }))

const { useConfirmDialog, CONFIRM_DIALOG_Z_INDEX }
  = await import('@/composables/useConfirmDialog')

function source(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8')
}

/** 這個元件用到的最高一層 z-index。 */
function highestZIndex(relative: string): number {
  const matches = source(relative).matchAll(/^\s*z-index:\s*(\d+);/gm)
  const values = [...matches].map(match => Number(match[1]))
  expect(values.length).toBeGreaterThan(0)
  return Math.max(...values)
}

describe('確認框疊在所有自訂 overlay 之上', () => {
  it('Modal.confirm 帶著 zIndex 常數，不是靠 ant-design 預設的 1000', () => {
    confirmSpy.mockClear()
    void useConfirmDialog()({ content: 'x' })

    expect(confirmSpy).toHaveBeenCalledTimes(1)
    expect(confirmSpy.mock.calls[0][0]).toMatchObject({
      zIndex: CONFIRM_DIALOG_Z_INDEX,
    })
  })

  it('常數高過 ant-design-vue 的 zIndexPopupBase 預設（本專案沒有覆寫它）', () => {
    expect(CONFIRM_DIALOG_Z_INDEX).toBeGreaterThan(1000)
  })

  const overlays: Array<[string, string]> = [
    // 直接肇事者：精靈送出後仍開著，覆蓋確認就是在它之上被喚起的。
    ['創角精靈', '../src/components/InitialRelationshipWizardModal.vue'],
    // 同型風險的第二處：手動建角視窗（FIX-A 之後）也會在自己還開著的時候
    // 喚起同一個覆蓋確認。
    ['手動建角視窗', '../src/components/CharacterCreateModal.vue'],
    ['ArcTemplateIntakeWizard', '../src/components/ArcTemplateIntakeWizard.vue'],
    ['ArcTemplatePicker', '../src/components/ArcTemplatePicker.vue'],
    ['UiLightbox', '../src/components/ui/UiLightbox.vue'],
    ['StoryArcPanel', '../src/components/StoryArcPanel.vue'],
    ['CharacterImagesPanel', '../src/components/CharacterImagesPanel.vue'],
  ]

  for (const [name, file] of overlays) {
    it(`${name} 疊不過確認框`, () => {
      expect(highestZIndex(file)).toBeLessThan(CONFIRM_DIALOG_Z_INDEX)
    })
  }
})
