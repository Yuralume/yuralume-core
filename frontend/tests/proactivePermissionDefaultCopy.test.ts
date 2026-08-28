import { describe, expect, it } from 'vitest'

import { messages as enUS } from '@/i18n/locales/en-US'
import { messages as jaJP } from '@/i18n/locales/ja-JP'
import { messages as zhTW } from '@/i18n/locales/zh-TW'

// TR2-B：「可以主動找我」翻成預設開之後，勾選框旁邊的說明就從「選填提示」
// 變成**告知**——預設是開的、可以當場取消、不會馬上來、以及網頁送不到的
// 那半段要靠綁定官方 LINE（NF 三層階梯的送達通道區隔，本票只接文案）。
//
// 這幾件事沒有任何一件會因為漏掉而讓測試變紅，只會在玩家被角色嚇到、或
// 以為自己沒被找過的時候才被發現，所以在這裡釘住三語都有這段說明。

const catalogues = {
  'zh-TW': zhTW,
  'en-US': enUS,
  'ja-JP': jaJP,
} as const

function permissionCopy(catalogue: typeof zhTW) {
  const section = (catalogue as Record<string, any>).characterCreate.initialRelationship
  return {
    label: section.proactivePermission as string,
    hint: section.proactivePermissionHint as string,
  }
}

describe('create-time proactive permission copy', () => {
  for (const [locale, catalogue] of Object.entries(catalogues)) {
    it(`tells the player the default is on and how to opt out (${locale})`, () => {
      const { label, hint } = permissionCopy(catalogue as typeof zhTW)

      expect(label.trim().length).toBeGreaterThan(0)
      expect(hint.trim().length).toBeGreaterThan(0)
    })

    it(`points at the LINE delivery channel from the NF ladder (${locale})`, () => {
      expect(permissionCopy(catalogue as typeof zhTW).hint).toContain('LINE')
    })
  }
})
