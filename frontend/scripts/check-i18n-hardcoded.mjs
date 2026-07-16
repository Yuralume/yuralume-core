import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const projectRoot = path.resolve(import.meta.dirname, '..')

/**
 * Files whose visible chrome has been migrated to vue-i18n.
 *
 * Keep this list intentionally narrow: it is a hard gate for areas we
 * claim are done, not a noisy whole-repo audit. As future phases
 * finish, add their files here.
 */
const completedFiles = [
  'src/App.vue',
  'src/layouts/AdminLayout.vue',
  'src/pages/LoginPage.vue',
  'src/pages/SetupPage.vue',
  'src/pages/admin/AdminHomePage.vue',
  'src/pages/admin/AdminPlaceholder.vue',
  'src/pages/admin/CharactersAdminPage.vue',
  'src/pages/admin/DispositionAdminPage.vue',
  'src/pages/admin/FollowUpsAdminPage.vue',
  'src/pages/admin/ImageProfilesAdminPage.vue',
  'src/pages/admin/LorasAdminPage.vue',
  'src/pages/admin/MemoriesAdminPage.vue',
  'src/pages/admin/ModelsAdminPage.vue',
  'src/pages/admin/ObservabilityAdminPage.vue',
  'src/pages/admin/ProactiveAdminPage.vue',
  'src/pages/admin/ScheduleAdminPage.vue',
  'src/pages/admin/UsersAdminPage.vue',
  'src/pages/admin/VideoProfilesAdminPage.vue',
  'src/pages/admin/VoiceAdminPage.vue',
  'src/pages/admin/WorldAdminPage.vue',
  'src/pages/StagePage.vue',
  'src/pages/StyleGuidePage.vue',
  'src/components/admin/AdminCharacterPicker.vue',
  'src/components/admin/DispositionAdminEditor.vue',
  'src/components/admin/DispositionDriftHistoryPanel.vue',
  'src/components/admin/AdminScopeSelector.vue',
  'src/components/admin/ProactiveAdminEditor.vue',
  'src/components/admin/WorldAdminEditor.vue',
  'src/components/AlbumPanel.vue',
  'src/components/ArcTemplateIntakeWizard.vue',
  'src/components/ArcTemplatePicker.vue',
  'src/components/ChannelAccountNextStep.vue',
  'src/components/ChannelBindingsPanel.vue',
  'src/components/ChannelProactiveAttemptLog.vue',
  'src/components/ChannelSetupGuide.vue',
  'src/components/CharacterRelationshipsPanel.vue',
  'src/components/CharacterCreateModal.vue',
  'src/components/CharacterEditPanel.vue',
  'src/components/CharacterCardFace.vue',
  'src/components/CharacterCardGalleryModal.vue',
  'src/components/CharacterIdentityFields.vue',
  'src/components/CharacterImagesPanel.vue',
  'src/components/CharacterRelationshipMood.vue',
  'src/components/CharacterLorasPanel.vue',
  'src/components/FeatureModelsPicker.vue',
  'src/components/ImageProfilesPicker.vue',
  'src/components/InterestSubscriptionPanel.vue',
  'src/components/MemoryBrowserPanel.vue',
  'src/components/OperatorPersonaPanel.vue',
  'src/components/PendingFollowUpsPanel.vue',
  'src/components/SimpleImageProfilePicker.vue',
  'src/components/SimpleVoicePicker.vue',
  'src/components/VisualGenerationStyleSetting.vue',
  'src/components/SchedulePanel.vue',
  'src/components/StoryArcPanel.vue',
  'src/components/StoryPanel.vue',
  'src/components/VideoProfilesPicker.vue',
  'src/components/VoiceProfilePanel.vue',
  'src/components/WorldAwarenessPanel.vue',
  'src/components/branching-drama/BranchingDramaPlayer.vue',
  'src/components/branching-drama/BranchingDramaStatusBadge.vue',
  'src/components/fusion-story/CharacterMultiSelect.vue',
  'src/components/fusion-story/FusionStoryExitHub.vue',
  'src/components/fusion-story/FusionStoryStatusBadge.vue',
  'src/components/fusion-story/FusionStoryViewer.vue',
  'src/components/FeedCard.vue',
  'src/components/FeedPanel.vue',
  'src/components/ImageStage.vue',
  'src/components/KokoroGramOverlay.vue',
  'src/components/observability/ObservabilityPanel.vue',
  'src/components/PlayerFollowUpsCard.vue',
  'src/components/PlayerGoalsPanel.vue',
  'src/components/PlayerCharacterCardPanel.vue',
  'src/components/PlayerEmptyState.vue',
  'src/components/PlayerOnboardingGuide.vue',
  'src/components/PlayerPasswordPanel.vue',
  'src/components/PlayerScheduleCard.vue',
  'src/components/PostCreateChannelGuide.vue',
  'src/components/PlayerSidebar.vue',
  'src/pages/BranchingDramaPage.vue',
  'src/pages/FusionStoryPage.vue',
  'src/pages/MemoirPage.vue',
  'src/components/ChatBubble.vue',
  'src/components/ChatFirstTurnGuide.vue',
  'src/components/ChatPanel.vue',
  'src/utils/api/worldEvents.ts',
  // Plan #1/#13 root cause lived here, outside the old allowlist. The
  // presence-frame factories no longer hard-code a zh display_name, so
  // this file is now gated too (and src/types is scanned below).
  'src/types/chat.ts',
]

const hanPattern = /[\u3400-\u9fff]/

/**
 * Fullwidth CJK punctuation that the plain Han scan misses when it's
 * used to *concatenate* around interpolations \u2014 e.g. `` `\uff1a${label}` ``
 * or `list.join('\u3001')`. These read as Chinese typography to en/ja
 * operators even when no Han ideograph is present, so they must not be
 * hard-coded in migrated files. Covers colon, comma/enumeration, corner
 * quotes, fullwidth parens, semicolon, and the wave dash.
 */
const fullwidthPunctuationPattern = /[\uff1a\u3001\u300c\u300d\uff08\uff09\uff1b\u301c\u3002\uff01\uff1f\uff0c]/

function isCommentOnlyLine(line) {
  const trimmed = line.trim()
  return (
    trimmed.startsWith('//') ||
    trimmed.startsWith('/*') ||
    trimmed.startsWith('*') ||
    trimmed.startsWith('*/') ||
    trimmed.startsWith('<!--') ||
    trimmed.startsWith('-->')
  )
}

function stripJsBlockComments(line, inBlockComment) {
  let remaining = line
  let inComment = inBlockComment
  let output = ''

  while (remaining.length > 0) {
    if (inComment) {
      const end = remaining.indexOf('*/')
      if (end === -1) {
        return { text: output, inBlockComment: true }
      }
      remaining = remaining.slice(end + 2)
      inComment = false
      continue
    }

    const start = remaining.indexOf('/*')
    if (start === -1) {
      output += remaining
      break
    }

    output += remaining.slice(0, start)
    remaining = remaining.slice(start + 2)
    inComment = true
  }

  return { text: output, inBlockComment: inComment }
}

const violations = []

for (const relative of completedFiles) {
  const absolute = path.join(projectRoot, relative)
  const text = fs.readFileSync(absolute, 'utf8')
  let inHtmlComment = false
  let inJsBlockComment = false
  text.split(/\r?\n/).forEach((line, index) => {
    const trimmed = line.trim()
    const startsHtmlComment = trimmed.startsWith('<!--')
    const endsHtmlComment = trimmed.endsWith('-->')
    if (startsHtmlComment && !endsHtmlComment) {
      inHtmlComment = true
    }
    const commentLine = inHtmlComment || startsHtmlComment || isCommentOnlyLine(line)
    if (inHtmlComment && endsHtmlComment) {
      inHtmlComment = false
    }
    const stripped = stripJsBlockComments(line, inJsBlockComment)
    inJsBlockComment = stripped.inBlockComment
    const scanText = commentLine ? '' : stripped.text.replace(/\/\/.*$/, '')
    if (commentLine) return
    if (hanPattern.test(scanText)) {
      violations.push(`${relative}:${index + 1}: ${line.trim()}`)
      return
    }
    // Fullwidth-punctuation concatenation: a migrated file must not
    // hard-code CJK typography even without Han ideographs.
    if (fullwidthPunctuationPattern.test(scanText)) {
      violations.push(
        `${relative}:${index + 1}: [fullwidth punctuation] ${line.trim()}`,
      )
    }
  })
}

if (violations.length > 0) {
  console.error(
    'Hard-coded CJK text / fullwidth punctuation found in completed i18n files:',
  )
  for (const line of violations) console.error(`  ${line}`)
  process.exit(1)
}

console.log(`i18n hard-coded text check passed (${completedFiles.length} files).`)
