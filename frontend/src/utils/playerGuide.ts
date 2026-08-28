/**
 * 玩家主介面「怎麼玩」導覽的章節骨架與一次性 coachmark 狀態（PG1）。
 *
 * 章節的順序、每章有哪幾條、以及哪幾條要看站別換句子，全部定死在這個
 * 模組的 constant 裡，而不是從 i18n 物件的鍵序推出來——catalogue 只准放
 * 字串葉子（陣列會被 `check:i18n` 擋下），而物件的鍵序不是可靠的順序來
 * 源。三語的鍵集合一致由 catalogue 檢查保證，constant 與鍵的一一對應由
 * `tests/playerGuide.test.ts` 釘住。
 *
 * `ready` 是「這一章寫完了沒」的閘：翻成 false 的章不會出現在目錄、也不
 * 會渲染任何一個字——新章在內容寫好之前就是這樣藏起來，導覽裡不該出現
 * 「敬請期待」這種佔位文案。十章目前全部 `ready: true`。
 */

export interface PlayerGuideItem {
  id: string
  /**
   * 這一條在 hosted 與自架讀起來不一樣（多半是扣點），所以除了
   * `items.<id>` 之外還有一個 `items.<id>SelfHost` 兄弟鍵。
   */
  siteAware?: true
  /**
   * 這一條講的能力只有自架站有，雲端整句不渲染（**沒有**兄弟鍵——雲端
   * 沒有可以替代的說法，硬寫一句「你的站沒有這個」等於把不存在的東西教
   * 給玩家）。與 `siteAware` 正交：那個是換句子，這個是整句消失。
   */
  selfHostOnly?: true
}

export interface PlayerGuideChapter {
  key: string
  icon: string
  /** 內容寫完了沒。false ＝ 骨架，完全不渲染。 */
  ready: boolean
  /** 只有雲端版有這個能力，自架整章不渲染。 */
  hostedOnly?: true
  /** 只有自架有這個能力，雲端整章不渲染。 */
  selfHostOnly?: true
  /**
   * 這一章結尾多一顆按鈕，直接打開既有的「創作區怎麼玩」導覽（PG2）。
   * 創作區的說明只維護在 `StudioGuideModal` 那一份——在這裡抄第二份，
   * 兩份就會從第一次改動開始各說各話。
   */
  opensStudioGuide?: true
  items: readonly PlayerGuideItem[]
}

/**
 * 十章的骨架：章節順序、每章有哪幾條、以及各自的條件化旗標。
 */
export const PLAYER_GUIDE_CHAPTERS: readonly PlayerGuideChapter[] = [
  {
    key: 'basics',
    icon: '✎',
    ready: true,
    items: [
      { id: 'action' },
      { id: 'pic', siteAware: true },
      { id: 'modes' },
      { id: 'attachImage' },
      // 語音播放是唯一「按了會扣點、旁邊卻沒有價籤」的玩家面按鈕（它按
      // 長度計價，不是固定價），所以扣點這件事只有在這裡講才會被看到。
      { id: 'voice', siteAware: true },
      { id: 'assist', siteAware: true },
      { id: 'undo' },
      { id: 'history' },
      // 自架限定：雲端沒有這個開關（`PersonalSettingsSection` 的那一段
      // 整個 `v-if="!cloudMode"`），所以雲端連提都不提。
      { id: 'nsfw', selfHostOnly: true },
    ],
  },
  {
    key: 'story',
    icon: '✦',
    ready: true,
    items: [
      { id: 'sceneOpen' },
      { id: 'sceneChips' },
      { id: 'sceneCanon' },
      { id: 'sceneEnd' },
      { id: 'scenePrice', siteAware: true },
      { id: 'nudge', siteAware: true },
    ],
  },
  {
    key: 'life',
    icon: '◷',
    ready: true,
    items: [
      { id: 'schedule' },
      { id: 'busyDefer' },
      { id: 'promise' },
      { id: 'proactive' },
      { id: 'noReply' },
      { id: 'drift' },
      { id: 'encounter' },
      { id: 'world' },
    ],
  },
  {
    key: 'lumegram',
    icon: '◎',
    ready: true,
    items: [
      { id: 'posts' },
      { id: 'comments' },
      { id: 'replyDelay' },
      { id: 'notify' },
      { id: 'private' },
    ],
  },
  {
    // 看圖是**跨面板**的一種操作，不是某一個功能的一部分：同一個浮窗掛在相簿、
    // 聊天、動態牆、舞台圖、生成候選圖與角色卡卡面六處。與其把「怎麼翻、怎麼
    // 關、原檔在哪」在四章各寫一次（四份會從第一次改動開始各說各話），不如給
    // 它一章——玩家想查的問題本來就是「大圖要怎麼看」，而不是「相簿的圖要怎麼
    // 看」。
    key: 'images',
    icon: '▣',
    ready: true,
    items: [
      { id: 'open' },
      { id: 'close' },
      { id: 'scope' },
      { id: 'original' },
      { id: 'stage' },
      { id: 'candidates' },
      { id: 'cards' },
    ],
  },
  {
    key: 'memory',
    icon: '❖',
    ready: true,
    items: [
      { id: 'memoir' },
      { id: 'throughEyes' },
      { id: 'callMeThat' },
      { id: 'personaNote' },
      { id: 'perCharacter' },
    ],
  },
  {
    key: 'studio',
    icon: '◇',
    ready: true,
    opensStudioGuide: true,
    // 刻意只有一條：創作區的內容只維護在 `StudioGuideModal` 那一份，這裡
    // 一句話帶到、章尾那顆按鈕直接把它叫出來。
    items: [
      { id: 'entry' },
    ],
  },
  {
    key: 'channels',
    icon: '⇄',
    ready: true,
    items: [
      { id: 'bind' },
      { id: 'proactive' },
      { id: 'officialLine', siteAware: true },
      { id: 'ladder', siteAware: true },
    ],
  },
  {
    key: 'portability',
    icon: '⌸',
    ready: true,
    items: [
      { id: 'card' },
      // IC 系列（player_identity_card）：帶的是創角時的關係/人設欄位，不
      // 是角色本身，也不是檔案——與 .lumecard／.lumebackup 的檔案語意不
      // 同，但同樣是「把設定帶著走」的概念，所以緊接在 .lumecard 後面講
      // 清楚兩者的差別。三個入口一次講完（創角精靈帶入與存卡／角色設定
      // 頁回存／設定頁「個人」分頁管理），不分散進其他章。
      { id: 'identityCard' },
      // hosted 的匯出節流（以帳號計的滾動窗）在自架不存在，所以匯出這條
      // 也要換句子——自架版不談節流。
      { id: 'backup', siteAware: true },
      { id: 'password' },
      { id: 'restore', siteAware: true },
      { id: 'showcase' },
    ],
  },
  {
    key: 'credits',
    icon: '✧',
    ready: true,
    hostedOnly: true,
    items: [
      { id: 'whenCharged' },
      { id: 'priceTag' },
      { id: 'balance' },
      { id: 'insufficient' },
      { id: 'overage' },
    ],
  },
]

/**
 * 這個站看得到哪幾章。
 *
 * 章節級條件化是 hard gate：hosted-only 的章在自架完全不渲染，連目錄都
 * 沒有它——一章談的整組能力在這個部署根本不存在時，用一句「你的站沒有
 * 這個」帶過只是把不存在的東西教給玩家。
 */
export function visiblePlayerGuideChapters(
  cloudMode: boolean,
): PlayerGuideChapter[] {
  return PLAYER_GUIDE_CHAPTERS.filter((chapter) => {
    if (!chapter.ready) return false
    if (chapter.hostedOnly && !cloudMode) return false
    if (chapter.selfHostOnly && cloudMode) return false
    return true
  })
}

/**
 * 一次性 coachmark：指向聊天 header 的「怎麼玩」入口。
 *
 * 與 `utils/arcDiscovery.ts` 那三顆同一套慣例（user-wide、localStorage、
 * 讀寫都 fail soft），但刻意用自己的鍵：開過創作區導覽不代表看過玩家導
 * 覽，共用一顆鍵會讓其中一課無聲吃掉另一課。
 */
export const PLAYER_GUIDE_COACHMARK_KEY =
  'yuralume.stage.guideCoachmark.dismissed'

type GuideStorage = Pick<Storage, 'getItem' | 'setItem'>

export function isPlayerGuideCoachmarkDismissed(
  storage: GuideStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    return storage.getItem(PLAYER_GUIDE_COACHMARK_KEY) === '1'
  } catch {
    return false
  }
}

export function rememberPlayerGuideCoachmarkDismissed(
  storage: GuideStorage | null | undefined,
): boolean {
  if (!storage) return false
  try {
    storage.setItem(PLAYER_GUIDE_COACHMARK_KEY, '1')
    return true
  } catch {
    return false
  }
}
