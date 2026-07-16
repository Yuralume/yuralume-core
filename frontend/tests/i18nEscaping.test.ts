import { describe, expect, it } from 'vitest'

import { i18n } from '@/i18n'

// Regression: vue-i18n treats `{ }`, `@`, `|` as message syntax. Messages that
// embed literal JSON (`{"top_k": 40}`) or a WhatsApp JID (`...@s.whatsapp.net`)
// must escape those with `{'{'}` / `{'@'}`, or t() throws a compile SyntaxError
// at render time and blanks the whole subtree (this crashed the Provider keys
// create form and the channel-bindings panel). These assert the escaped
// messages both compile AND render back to the intended literal text.
const LOCALES = ['en-US', 'zh-TW', 'ja-JP'] as const

function render(key: string): string {
  return i18n.global.t(key)
}

describe('i18n special-character escaping renders literally', () => {
  it('extra_request_params placeholder renders literal JSON braces in every locale', () => {
    for (const locale of LOCALES) {
      i18n.global.locale.value = locale
      expect(
        render('admin.providerSettings.providerFields.extra_request_params.placeholder'),
        `extra_request_params placeholder broken in ${locale}`,
      ).toContain('{"top_k": 40}')
    }
  })

  it('WhatsApp JID placeholders render a literal @ in every locale', () => {
    for (const locale of LOCALES) {
      i18n.global.locale.value = locale
      expect(
        render('channelBindingsPanel.allowlist.whatsappPlaceholder'),
        `allowlist whatsappPlaceholder broken in ${locale}`,
      ).toContain('12025550123@s.whatsapp.net')
      expect(
        render('channelBindingsPanel.bindings.whatsappPlaceholder'),
        `bindings whatsappPlaceholder broken in ${locale}`,
      ).toContain('12025550123@s.whatsapp.net')
    }
  })
})
