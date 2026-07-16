import enUS from 'ant-design-vue/es/locale/en_US'
import jaJP from 'ant-design-vue/es/locale/ja_JP'
import zhTW from 'ant-design-vue/es/locale/zh_TW'

import type { SupportedLocale } from './localeTypes'

export const antDesignLocales: Record<SupportedLocale, typeof zhTW> = {
  'zh-TW': zhTW,
  'en-US': enUS,
  'ja-JP': jaJP,
}
