import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import ts from 'typescript'
import { baseCompile } from '@intlify/message-compiler'

const projectRoot = path.resolve(import.meta.dirname, '..')

async function importTs(relativePath) {
  const absolutePath = path.join(projectRoot, relativePath)
  const source = fs.readFileSync(absolutePath, 'utf8')
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2022,
      importsNotUsedAsValues: ts.ImportsNotUsedAsValues.Remove,
    },
    fileName: absolutePath,
  }).outputText
  const encoded = Buffer.from(transpiled, 'utf8').toString('base64')
  return import(`data:text/javascript;base64,${encoded}`)
}

function flatten(value, prefix = '') {
  if (typeof value === 'string') return [[prefix, value]]
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return [[prefix, value]]
  }
  return Object.entries(value).flatMap(([key, child]) => {
    const next = prefix ? `${prefix}.${key}` : key
    return flatten(child, next)
  })
}

function assertSameShape(source, target, localeName) {
  const sourceKeys = new Set(flatten(source).map(([key]) => key))
  const targetKeys = new Set(flatten(target).map(([key]) => key))
  const missing = [...sourceKeys].filter((key) => !targetKeys.has(key))
  const extra = [...targetKeys].filter((key) => !sourceKeys.has(key))
  if (missing.length || extra.length) {
    throw new Error([
      'Locale catalog shape mismatch.',
      missing.length ? `Missing ${localeName} keys:\n${missing.map((key) => `  - ${key}`).join('\n')}` : '',
      extra.length ? `Extra ${localeName} keys:\n${extra.map((key) => `  - ${key}`).join('\n')}` : '',
    ].filter(Boolean).join('\n'))
  }
}

function assertNoEmptyOrTodo(localeName, catalog) {
  const violations = flatten(catalog).flatMap(([key, value]) => {
    if (typeof value !== 'string') return [`${key}: non-string leaf`]
    if (value.trim() === '') return [`${key}: empty string`]
    if (value.includes('TODO_TRANSLATE')) return [`${key}: TODO_TRANSLATE marker`]
    return []
  })
  if (violations.length) {
    throw new Error([
      `${localeName} has invalid i18n leaves:`,
      ...violations.map((line) => `  - ${line}`),
    ].join('\n'))
  }
}

function assertJapaneseCatalogQuality(source, catalog) {
  const sourceLeaves = new Map(flatten(source))
  const identicalAllowedKeys = new Set([
    'common.actions.confirm',
    'common.fallback.notSet',
    'locale.location.latitude',
    'locale.location.latitudePlaceholder',
    'studio.series.premiseLabel',
    'chat.input.attachLimit',
    'chat.bubble.ttsStop',
    'feed.card.source.memory',
    'playerSidebar.tabs.settings',
    'playerSidebar.tabs.goals',
    'playerSchedule.fields.start',
    'story.arcPanel.newArc.start',
    'memoir.kind.memory',
    'branchingDrama.actions.start',
    'observability.sections.settings',
    'characterEdit.identityWarningLabel',
    'voiceProfilePanel.hints.catalogMissingPrefix',
    'characterLorasPanel.strengthLabel',
    'schedulePanel.fields.start',
    'observabilityPanel.tabs.settings',
    'observabilityPanel.usage.capability',
    'admin.home.statusInProgress',
    'admin.home.entries.memories.title',
    'admin.page.memories.title',
    'admin.providerSettings.fields.capabilities',
    'admin.nav.memories',
    // Natural Japanese that legitimately shares kanji / loan-word form with
    // the zh-TW source (Sino-Japanese vocabulary, shared idioms, or ASCII
    // placeholders). Identical form is correct here — not a missing
    // translation.
    'admin.home.entries.proactive.title',
    'admin.page.proactive.title',
    'admin.providerSettings.capabilityCardTitle',
    'admin.providerSettings.partialFailure',
    'admin.usersAdmin.displayNamePlaceholder',
    'branchingDrama.page.segmentCount',
    'branchingDrama.page.segmentCountCompact',
    'branchingDrama.page.sessionPlaying',
    'branchingDrama.page.sessionProgress',
    'branchingDrama.status.playing',
    'channelBindingsPanel.create.displayNamePlaceholder',
    'channelBindingsPanel.publicBaseUrl.source.empty',
    'characterCreate.draft.generating',
    'characterCreate.fields.worldFrame.options.modern',
    'characterEdit.links.proactive.link',
    'characterEdit.reset.deleted.memories',
    'characterEdit.state.affection',
    'characterImagesPanel.generate.generating',
    'characterImagesPanel.generate.placeholder',
    'characterRelationshipsPanel.manualTick',
    'characterRelationshipsPanel.meta.recent',
    'characterRelationshipsPanel.status.running',
    'feed.card.kind.daily',
    'feed.card.kind.external',
    'feed.card.kind.work',
    'feed.card.source.manual',
    'feed.card.source.stateShift',
    'feed.card.source.worldEvent',
    'fusionStory.status.ready',
    'fusionStory.viewer.beatLength',
    'fusionStory.viewer.cast',
    'interestSubscriptionPanel.categories.culture.label',
    'interestSubscriptionPanel.categories.health.label',
    'locale.location.labelPlaceholder',
    'memoir.chapters.quotesLabel',
    'memoir.focus.intensityLabel',
    'memoir.knownFacts.score',
    'memoir.timeline.score',
    'memoryBrowser.salienceLabel',
    'memoryBrowser.title',
    'nsfwModeSetting.status.activeWithMinutes',
    'nsfwModeTargetSetting.status.unconfigured',
    'observabilityPanel.curiosity.columns.when',
    'observabilityPanel.emotions.energy',
    'observabilityPanel.emotions.intensity',
    // Cost-modeling: standard Sino-Japanese terms whose natural JP form is the
    // same kanji as zh-TW (差額 = balance/difference, 前景 / 背景 =
    // foreground / background, 保存 = save).
    'observabilityPanel.costModeling.delta',
    'observabilityPanel.costModeling.foreground',
    'observabilityPanel.costModeling.background',
    'observabilityPanel.costModeling.save',
    'operatorPersona.fields.occupation',
    'operatorPersona.fields.residence',
    'operatorPersona.fields.secrets',
    'operatorPersona.source.explicit',
    'playerFollowUps.adminLink',
    'playerGoals.create.priority',
    'playerGoals.origin.manual',
    'playerGoals.priorityBadge',
    'playerGoals.status.active',
    'playerGoals.title',
    'playerSchedule.nowBadge',
    'playerSidebar.characterCards.details.fields.personality',
    'playerSidebar.settings.scope.personal',
    'relationshipMood.affectionAria',
    'schedulePanel.participants.companions',
    'story.arcPanel.sceneType.conflict',
    'story.arcPanel.status.active',
    'story.arcPanel.status.completed',
    'story.arcTemplateIntake.beats.suggesting',
    'story.arcTemplateIntake.pitch.suggesting',
    'story.arcTemplateIntake.review.worldFramesLabel',
    'story.arcTemplateIntake.sceneType.conflict',
    'story.arcTemplatePicker.current',
    'story.arcTemplatePicker.sceneType.conflict',
    'story.panel.worldFrameOptions.modern',
    'studio.series.memberCount',
    'worldAwarenessPanel.topicPlaceholder',
  ])
  const bannedPatterns = [
    /について確認し、必要に応じて設定してください/,
    /の内容を確認し、必要に応じて設定してください/,
    /プレースホルダーを入力/,
    /空はまだありません/,
    /(?:パネル|Profile選択|Arcテンプレート|チャンネル紐づけパネル)に失敗しました/,
    /失敗に失敗しました/,
    /每日/,
    /[沒們這麼]/,
    /，/,
  ]
  const violations = flatten(catalog).flatMap(([key, value]) => {
    if (typeof value !== 'string') return []
    const issues = []
    for (const pattern of bannedPatterns) {
      if (pattern.test(value)) {
        issues.push(`${key}: suspicious Japanese copy "${value}"`)
        break
      }
    }
    const sourceValue = sourceLeaves.get(key)
    if (
      typeof sourceValue === 'string'
      && sourceValue === value
      && /[\u3400-\u9fff]/.test(value)
      && value.length > 1
      && !identicalAllowedKeys.has(key)
    ) {
      issues.push(`${key}: identical to zh-TW source "${value}"`)
    }
    return issues
  })
  if (violations.length) {
    throw new Error([
      'ja-JP has suspicious translation leaves:',
      ...violations.map((line) => `  - ${line}`),
    ].join('\n'))
  }
}

function assertCompilable(localeName, catalog) {
  // vue-i18n compiles every message through its own mini-syntax where `{ }`,
  // `@`, and `|` are special. A leaf that contains an unescaped literal brace
  // (e.g. a JSON example "{\"top_k\": 40}") throws a SyntaxError at t() time,
  // which blanks the whole render subtree in production. The other gates never
  // compile messages, so this class of bug shipped unnoticed. Compile every
  // string leaf here and fail loudly with the exact key.
  const violations = flatten(catalog).flatMap(([key, value]) => {
    if (typeof value !== 'string') return []
    try {
      baseCompile(value, { onError(err) { throw err } })
      return []
    } catch (err) {
      const message = err instanceof Error ? err.message.split('\n')[0] : String(err)
      return [`${key}: ${message} :: ${JSON.stringify(value)}`]
    }
  })
  if (violations.length) {
    throw new Error([
      `${localeName} has i18n messages vue-i18n cannot compile (t() would throw and blank the render):`,
      ...violations.map((line) => `  - ${line}`),
      'Escape literal braces with vue-i18n syntax, e.g. {\'{\'}"top_k": 40{\'}\'}.',
    ].join('\n'))
  }
}

function assertIncludes(label, actual, expected) {
  if (!actual.includes(expected)) {
    throw new Error(`${label}: expected "${actual}" to include "${expected}"`)
  }
}

function assertNotIncludes(label, actual, forbidden) {
  if (actual.includes(forbidden)) {
    throw new Error(`${label}: expected "${actual}" not to include "${forbidden}"`)
  }
}

async function main() {
  const [{ messages: zhTW }, localeTypes, formatters] = await Promise.all([
    importTs('src/i18n/locales/zh-TW.ts'),
    importTs('src/i18n/localeTypes.ts'),
    importTs('src/i18n/formatters.ts'),
  ])

  const targetLocales = localeTypes.SUPPORTED_LOCALES.filter(
    (locale) => locale !== localeTypes.SOURCE_LOCALE,
  )
  const targetCatalogs = await Promise.all(targetLocales.map(async (locale) => {
    const module = await importTs(`src/i18n/locales/${locale}.ts`)
    return [locale, module.messages]
  }))

  assertNoEmptyOrTodo('zh-TW', zhTW)
  assertCompilable('zh-TW', zhTW)
  for (const [locale, catalog] of targetCatalogs) {
    assertSameShape(zhTW, catalog, locale)
    assertNoEmptyOrTodo(locale, catalog)
    assertCompilable(locale, catalog)
    if (locale === 'ja-JP') {
      assertJapaneseCatalogQuality(zhTW, catalog)
    }
  }

  const now = new Date('2026-05-22T12:00:00Z')
  const oneMinuteAgo = '2026-05-22T11:59:00Z'
  const zhRelative = formatters.formatRelativeTime(oneMinuteAgo, 'zh-TW', now)
  const enRelative = formatters.formatRelativeTime(oneMinuteAgo, 'en-US', now)
  const jaRelative = formatters.formatRelativeTime(oneMinuteAgo, 'ja-JP', now)
  assertIncludes('zh-TW relative time', zhRelative, '分')
  assertNotIncludes('zh-TW relative time', zhRelative, 'minute')
  assertIncludes('en-US relative time', enRelative, 'minute')
  assertNotIncludes('en-US relative time', enRelative, '分鐘')
  assertIncludes('ja-JP relative time', jaRelative, '分')
  assertNotIncludes('ja-JP relative time', jaRelative, 'minute')
  assertNotIncludes('ja-JP relative time', jaRelative, '分鐘')

  const zhDuration = formatters.formatDurationMinutes(65, 'zh-TW')
  const enDuration = formatters.formatDurationMinutes(65, 'en-US')
  const jaDuration = formatters.formatDurationMinutes(65, 'ja-JP')
  const jaShortDuration = formatters.formatDurationMinutes(5, 'ja-JP')
  if (zhDuration !== '1 小時 5 分') {
    throw new Error(`zh-TW duration: expected "1 小時 5 分", got "${zhDuration}"`)
  }
  if (enDuration !== '1h 5m') {
    throw new Error(`en-US duration: expected "1h 5m", got "${enDuration}"`)
  }
  if (jaDuration !== '1時間 5分') {
    throw new Error(`ja-JP duration: expected "1時間 5分", got "${jaDuration}"`)
  }
  if (jaShortDuration !== '5分') {
    throw new Error(`ja-JP duration: expected "5分", got "${jaShortDuration}"`)
  }

  console.log('i18n catalog and formatter checks passed.')
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err)
  process.exit(1)
})
