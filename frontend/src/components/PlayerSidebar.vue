<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/composables/useAuth'
import { useLocale } from '@/composables/useLocale'
import { buildInfoTitle, formatBuildVersion } from '@/utils/buildInfo'
import { resolveNeedsProviderSetup } from '@/utils/providerSetup'
import type { Character } from '@/types/character'
import {
  getCharacter,
  updateCharacter,
} from '@/utils/api/characters'
import { characterDisplayRef } from '@/utils/characterDisplay'
import { useConfirmDialog } from '@/composables/useConfirmDialog'
import type { MessagingPlatform } from '@/types/messaging'
import { resolveWebPushNudge } from '@/utils/webPushNudge'
import SidebarBrand from './SidebarBrand.vue'
import PostCreateChannelGuide from './PostCreateChannelGuide.vue'
import AlbumPanel from './AlbumPanel.vue'
import CharacterImagesPanel from './CharacterImagesPanel.vue'
import CharacterCreateModal from './CharacterCreateModal.vue'
import PlayerEmptyState from './PlayerEmptyState.vue'
import PlayerOnboardingGuide from './PlayerOnboardingGuide.vue'
import PlayerProviderSetupGuide from './PlayerProviderSetupGuide.vue'
import PlayerCharacterCardPanel from './PlayerCharacterCardPanel.vue'
import StoryPanel from './StoryPanel.vue'
import ArcDiscoveryCard from './ArcDiscoveryCard.vue'
import CollapsibleSection from './CollapsibleSection.vue'
import PlayerSettingsPanel from './PlayerSettingsPanel.vue'
import PlayerScheduleCard from './PlayerScheduleCard.vue'
import PlayerFollowUpsCard from './PlayerFollowUpsCard.vue'
import PlayerGoalsPanel from './PlayerGoalsPanel.vue'
import CharacterRelationshipMood from './CharacterRelationshipMood.vue'
import WorldAdminEditor from './admin/WorldAdminEditor.vue'

const props = defineProps<{
  characters: Character[]
  selectedCharacter: Character | null
}>()

const emit = defineEmits<{
  selectCharacter: [char: Character]
  characterUpdated: [char: Character]
  characterCreated: [char: Character]
  deleteCharacter: [char: Character]
  characterDataReset: [char: Character]
  openMemoir: [char: Character]
}>()

const { t } = useI18n()
const router = useRouter()
const { locale, supported } = useLocale()
const {
  isAdmin,
  cloudMode,
  buildInfo,
} = useAuth()
const confirmDialog = useConfirmDialog()
const showAdminEntrances = computed(() => isAdmin.value)

// 自架首次部署：玩家落在玩家頁，但若還沒接上任何真實 LLM provider 聊天會跑不起來。
// 只對能進後台處理的管理者顯示引導（非管理者沒有後台入口，提示也無從操作）。
const providerSetupNeeded = ref(false)
const showProviderSetupGuide = computed(() => isAdmin.value && providerSetupNeeded.value)

onMounted(async () => {
  providerSetupNeeded.value = await resolveNeedsProviderSetup(cloudMode.value)
})

const buildVersionLabel = computed(() => formatBuildVersion(buildInfo.value))
const buildVersionTitle = computed(() => buildInfoTitle(buildInfo.value))
const selectedCharacterDisplayName = computed(() => (
  characterDisplayRef(props.selectedCharacter, t('common.character'))
))
const selectedCharacterHasActiveStoryArc = computed(() => {
  if (!props.selectedCharacter) return null
  return lifeStoryHasActiveArcByCharacter.value[props.selectedCharacter.id] ?? null
})
const shouldShowStorySectionTeaser = computed(() => {
  if (!props.selectedCharacter) return false
  if (selectedCharacterHasActiveStoryArc.value !== false) return false
  return !props.selectedCharacter.arc_template_id && !props.selectedCharacter.arc_series_id
})
// Unknown (the story panel has not reported yet) is treated as "has arc" so the
// hoisted discovery card stays hidden until we positively know the character is
// empty, avoiding a flash before the panel loads.
const lifeStoryHasActiveArc = computed(() => {
  if (!props.selectedCharacter) return true
  return lifeStoryHasActiveArcByCharacter.value[props.selectedCharacter.id] ?? true
})
async function requestDelete(char: Character, event: Event) {
  event.stopPropagation()
  if (!await confirmDialog({
    content: t('playerSidebar.deleteConfirm', { name: char.name }),
    okText: t('common.actions.delete'),
    danger: true,
  })) return
  emit('deleteCharacter', char)
}

const SIDEBAR_TABS = [
  { key: 'characters', labelKey: 'playerSidebar.tabs.characters', requiresCharacter: false },
  { key: 'life', labelKey: 'playerSidebar.tabs.life', requiresCharacter: true },
  { key: 'memories', labelKey: 'playerSidebar.tabs.memories', requiresCharacter: true },
  { key: 'settings', labelKey: 'playerSidebar.tabs.settings', requiresCharacter: false },
] as const

type SidebarTab = (typeof SIDEBAR_TABS)[number]['key']

const activeTab = ref<SidebarTab>('characters')

const worldFrame = ref('modern')
const worldFrameFeedback = ref<string | null>(null)
const lifeStoryHasActiveArcByCharacter = ref<Record<string, boolean>>({})
const lifeStoryPanel = ref<InstanceType<typeof StoryPanel> | null>(null)
const storySectionSignal = ref(0)

const createModalOpen = ref(false)
const postCreateChannelGuideCharacter = ref<Character | null>(null)
const emptyCharacterCardPanel = ref<InstanceType<typeof PlayerCharacterCardPanel> | null>(null)
const settingsPanel = ref<InstanceType<typeof PlayerSettingsPanel> | null>(null)

function openCreateModal() {
  createModalOpen.value = true
}

function closeCreateModal() {
  createModalOpen.value = false
}

function goToCharactersTab() {
  activeTab.value = 'characters'
}

async function openOnboardingCardBrowse() {
  activeTab.value = 'characters'
  await nextTick()
  void emptyCharacterCardPanel.value?.openBrowse()
}

function handleCharacterCreated(char: Character) {
  emit('characterCreated', char)
  postCreateChannelGuideCharacter.value = char
  createModalOpen.value = false
}

async function openChannelSetup(platform: MessagingPlatform) {
  postCreateChannelGuideCharacter.value = null
  activeTab.value = 'settings'
  await nextTick()
  await settingsPanel.value?.openChannelSetup(platform)
}

// 玩家在綁定通道引導選「稍後再說」：Web Push 無法預設打開（需使用者手勢授權），
// 退而提醒——若瀏覽器支援且尚未訂閱，就帶去個人設定並閃一下系統推播開關。
async function dismissPostCreateChannelGuide() {
  postCreateChannelGuideCharacter.value = null
  if (!await resolveWebPushNudge()) return
  activeTab.value = 'settings'
  await nextTick()
  await settingsPanel.value?.highlightWebNotification()
}

// provider 引導第一階段：切到「設定」分頁，並把後台入口閃一下，讓使用者學會
// 下次從哪裡進管理後台。第二階段（後台首頁 → Provider Keys）由 AdminHome 既有的
// needsProviderSetup 卡片接手。
async function goToProviderSetup() {
  activeTab.value = 'settings'
  await nextTick()
  await settingsPanel.value?.highlightAdminEntry()
}

async function handleAlbumCharacterUpdated(characterId: string) {
  try {
    const fresh = await getCharacter(characterId)
    emit('characterUpdated', fresh)
  } catch {
    /* 非致命；下次 reload 角色時會拿到最新 */
  }
}

watch(() => props.selectedCharacter, (char) => {
  if (!char) {
    if (activeTab.value === 'life' || activeTab.value === 'memories') {
      activeTab.value = 'characters'
    }
    return
  }
  worldFrame.value = char.world_frame ?? 'modern'
  worldFrameFeedback.value = null
}, { immediate: true })

// 故事背景改為即時自動儲存：StoryPanel 在使用者切換時 emit
// update:world-frame，這裡直接 PATCH，不再需要獨立的儲存按鈕。
// 初始值由 watch(selectedCharacter) 帶入，不走這條路徑，因此不會
// 在載入當下回寫一次相同值。
async function onWorldFrameChange(value: string) {
  worldFrame.value = value
  if (!props.selectedCharacter) return
  worldFrameFeedback.value = null
  try {
    const updated = await updateCharacter(
      props.selectedCharacter.id,
      { world_frame: value },
    )
    emit('characterUpdated', updated)
    worldFrameFeedback.value = t('playerSidebar.errors.saved')
  } catch (err) {
    worldFrameFeedback.value = err instanceof Error
      ? t('common.errorWithDetail', { message: t('playerSidebar.errors.saveFailed'), detail: err.message })
      : t('playerSidebar.errors.saveFailed')
  }
}

async function handleArcTemplateChange(templateId: string | null) {
  if (!props.selectedCharacter) return
  try {
    const updated = await updateCharacter(
      props.selectedCharacter.id,
      { arc_template_id: templateId },
    )
    emit('characterUpdated', updated)
  } catch (err) {
    worldFrameFeedback.value = err instanceof Error
      ? t('common.errorWithDetail', { message: t('playerSidebar.errors.updateArcTemplateFailed'), detail: err.message })
      : t('playerSidebar.errors.updateArcTemplateFailed')
  }
}

function handleLifeStoryActiveArcChange(hasActiveArc: boolean) {
  if (!props.selectedCharacter) return
  lifeStoryHasActiveArcByCharacter.value = {
    ...lifeStoryHasActiveArcByCharacter.value,
    [props.selectedCharacter.id]: hasActiveArc,
  }
}

// The discovery card is hoisted above the (collapsed) story section, so its
// CTAs expand the section and drive the existing StoryArcPanel actions.
function handleArcDiscoveryStartLlm() {
  storySectionSignal.value += 1
  lifeStoryPanel.value?.openNewArc()
}

function handleArcDiscoveryPickTemplate() {
  storySectionSignal.value += 1
  lifeStoryPanel.value?.openTemplatePicker()
}

function handleArcDiscoveryOpenStudio() {
  router.push('/studio')
}

function handleEditPanelUpdated(char: Character) {
  emit('characterUpdated', char)
}

function handleEditPanelDataReset(char: Character) {
  emit('characterDataReset', char)
}

function sidebarTabLabel(tab: (typeof SIDEBAR_TABS)[number]): string {
  if (tab.key === 'life' && props.selectedCharacter) {
    return t('playerSidebar.tabs.lifeForCharacter', {
      name: selectedCharacterDisplayName.value,
    })
  }
  return t(tab.labelKey)
}

</script>

<template>
  <div class="sidebar-panel">
    <!-- 標題區 -->
    <SidebarBrand :subtitle="t('playerSidebar.brandSubtitle')" />

    <!-- 頁籤 -->
    <div class="tabs" role="tablist" :aria-label="t('playerSidebar.tabs.ariaLabel')">
      <button
        v-for="tab in SIDEBAR_TABS"
        :key="tab.key"
        :class="['tab', { active: activeTab === tab.key }]"
        :disabled="tab.requiresCharacter && !selectedCharacter"
        role="tab"
        :aria-selected="activeTab === tab.key"
        @click="activeTab = tab.key"
      >{{ sidebarTabLabel(tab) }}</button>
    </div>

      <!-- 角色列表 -->
      <div v-if="activeTab === 'characters'" class="tab-content">
        <PlayerProviderSetupGuide
          v-if="showProviderSetupGuide"
          @start="goToProviderSetup"
        />

        <PostCreateChannelGuide
          :character="postCreateChannelGuideCharacter"
          @setup="openChannelSetup"
          @dismiss="dismissPostCreateChannelGuide"
        />

        <div v-if="characters.length === 0" class="character-list">
          <PlayerOnboardingGuide
            @create="openCreateModal"
            @browse-cards="openOnboardingCardBrowse"
          />
          <CollapsibleSection
            :title="t('playerSidebar.characterCards.sectionTitle')"
            :hint="t('playerSidebar.characterCards.sectionHint')"
            :default-open="true"
          >
            <PlayerCharacterCardPanel
              ref="emptyCharacterCardPanel"
              :selected-character="selectedCharacter"
              @character-created="handleCharacterCreated"
            />
          </CollapsibleSection>
        </div>

        <template v-else>
        <div class="character-list">
        <div
          v-for="char in characters"
          :key="char.id"
          :class="['character-card', { selected: selectedCharacter?.id === char.id }]"
          @click="emit('selectCharacter', char)"
        >
          <div class="card-avatar">
            <img
              v-if="char.image_urls && char.image_urls.length > 0"
              :src="char.image_urls[0]"
              :alt="char.name"
              class="card-avatar-img"
              loading="lazy"
              @error="($event.target as HTMLImageElement).style.display = 'none'"
            />
            <span v-else class="card-avatar-letter">{{ char.name.charAt(0) }}</span>
            <span
              v-if="char.unread_proactive_count > 0"
              class="card-unread-badge"
              :title="t('playerSidebar.states.unreadProactiveAria', { count: char.unread_proactive_count })"
              :aria-label="t('playerSidebar.states.unreadProactiveAria', { count: char.unread_proactive_count })"
            >{{ char.unread_proactive_count > 99 ? '99+' : char.unread_proactive_count }}</span>
          </div>
          <div class="card-info">
            <div class="card-name">{{ char.name }}</div>
            <div class="card-meta">
              <CharacterRelationshipMood
                :emotion="char.state.emotion"
                :affection="char.state.affection"
                :energy="char.state.energy"
              />
            </div>
          </div>
          <button
            class="card-delete"
            :title="t('playerSidebar.actions.deleteCharacter')"
            :aria-label="t('playerSidebar.actions.deleteCharacter')"
            @click="requestDelete(char, $event)"
          >×</button>
        </div>
        </div>

        <button class="btn-new-char" @click="openCreateModal">
          {{ t('playerSidebar.actions.newCharacter') }}
        </button>

        <CollapsibleSection
          :title="t('playerSidebar.characterCards.sectionTitle')"
          :hint="t('playerSidebar.characterCards.sectionHint')"
          :default-open="false"
        >
          <PlayerCharacterCardPanel
            :selected-character="selectedCharacter"
            @character-created="handleCharacterCreated"
          />
        </CollapsibleSection>

      </template>

    </div>
    <!-- 角色生活：行程、故事、目標與待回覆都收在同一個玩家意圖下。 -->
    <div v-if="activeTab === 'life'" class="tab-content">
      <template v-if="selectedCharacter">
        <PlayerScheduleCard
          :character-id="selectedCharacter.id"
          :show-admin-link="showAdminEntrances"
        />

        <ArcDiscoveryCard
          :character="selectedCharacter"
          :has-active-arc="lifeStoryHasActiveArc"
          @start-llm="handleArcDiscoveryStartLlm"
          @pick-template="handleArcDiscoveryPickTemplate"
          @open-studio="handleArcDiscoveryOpenStudio"
        />

        <CollapsibleSection
          :title="t('playerSidebar.life.storySectionTitle')"
          :hint="shouldShowStorySectionTeaser ? t('playerSidebar.life.storySectionTeaser') : ''"
          :default-open="false"
          :open-signal="storySectionSignal"
        >
          <StoryPanel
            ref="lifeStoryPanel"
            :character-id="selectedCharacter.id"
            :world-frame="worldFrame"
            :arc-template-id="selectedCharacter.arc_template_id ?? null"
            @update:world-frame="onWorldFrameChange"
            @update:arc-template="handleArcTemplateChange"
            @active-arc-change="handleLifeStoryActiveArcChange"
          />
          <div v-if="worldFrameFeedback" class="reset-feedback">{{ worldFrameFeedback }}</div>
        </CollapsibleSection>

        <CollapsibleSection
          :title="t('playerSidebar.life.worldSectionTitle')"
          :hint="t('playerSidebar.life.worldSectionHint')"
          :default-open="false"
        >
          <WorldAdminEditor
            :key="`${selectedCharacter.id}:player-world`"
            :character="selectedCharacter"
            :patch="handleEditPanelUpdated"
            surface="player"
            :include-world-frame="false"
            :show-event-pool-preview="false"
          />
        </CollapsibleSection>

        <CollapsibleSection
          :title="t('playerSidebar.life.intentionsSectionTitle')"
          :default-open="false"
        >
          <PlayerGoalsPanel :key="selectedCharacter.id" :character="selectedCharacter" />
          <PlayerFollowUpsCard
            :character-id="selectedCharacter.id"
            :show-admin-link="showAdminEntrances"
          />
        </CollapsibleSection>
      </template>
      <PlayerEmptyState
        v-else
        icon="◷"
        :title="t('playerSidebar.emptyStates.selectCharacterTitle')"
        :hint="t('playerSidebar.emptyStates.lifeHint')"
        :action-label="t('playerSidebar.emptyStates.backToCharacters')"
        @action="goToCharactersTab"
      />
    </div>

    <!-- 回憶與相簿：回憶錄、舞台圖與相簿放在同一個瀏覽意圖下。 -->
    <div v-if="activeTab === 'memories'" class="tab-content">
      <template v-if="selectedCharacter">
        <div class="memoir-entry">
          <button
            type="button"
            class="memoir-link"
            @click="emit('openMemoir', selectedCharacter)"
          >
            <span class="memoir-icon" aria-hidden="true">📖</span>
            <span class="memoir-text">
              <span class="memoir-title">{{ t('playerSidebar.memoir.title') }}</span>
              <span class="memoir-sub">{{ t('playerSidebar.memoir.sub', { name: selectedCharacter.name }) }}</span>
            </span>
            <span class="memoir-arrow" aria-hidden="true">→</span>
          </button>
        </div>

        <CharacterImagesPanel
          :character="selectedCharacter"
          @updated="emit('characterUpdated', $event)"
        />
        <p class="admin-link-hint">
          {{ t('playerSidebar.album.triggerHint') }}
        </p>
        <CollapsibleSection :title="t('playerSidebar.album.sectionTitle')" :default-open="true">
          <AlbumPanel
            :character-id="selectedCharacter.id"
            @character-updated="handleAlbumCharacterUpdated"
          />
        </CollapsibleSection>
      </template>
      <PlayerEmptyState
        v-else
        icon="✦"
        :title="t('playerSidebar.emptyStates.selectCharacterTitle')"
        :hint="t('playerSidebar.emptyStates.memoriesHint')"
        :action-label="t('playerSidebar.emptyStates.backToCharacters')"
        @action="goToCharactersTab"
      />
    </div>

    <!-- 設定頁 -->
    <div v-if="activeTab === 'settings'" class="tab-content">
      <PlayerSettingsPanel
        ref="settingsPanel"
        :characters="characters"
        :selected-character="selectedCharacter"
        @character-updated="handleEditPanelUpdated"
        @character-data-reset="handleEditPanelDataReset"
        @create-character="openCreateModal"
      />
    </div>

    <!-- Sidebar 底部固定區：UI locale 切換器。獨立於 tab 內容，跨
         所有 tab 都看得到，跟 admin topbar 的切換器同一條 reactive
         state（i18n.global.locale），任一處切換另一處立即同步。 -->
    <footer class="sidebar-foot">
      <div
        v-if="buildVersionLabel"
        class="sidebar-version"
        :title="buildVersionTitle"
      >
        {{ buildVersionLabel }}
      </div>
      <label class="locale-switcher">
        <span class="locale-switcher__label">{{ t('locale.switcher.label') }}</span>
        <select
          v-model="locale"
          class="field-select locale-switcher__select"
          :title="t('locale.switcher.hint')"
          :aria-label="t('locale.switcher.label')"
        >
          <option v-for="opt in supported" :key="opt.code" :value="opt.code">
            {{ opt.label }}
          </option>
        </select>
      </label>
    </footer>

    <CharacterCreateModal
      v-if="createModalOpen"
      @close="closeCreateModal"
      @created="handleCharacterCreated"
    />
  </div>
</template>

<style scoped>
.sidebar-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.tabs {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-bottom: 1px solid var(--color-border);
}

.tab {
  min-width: 0;
  padding: 10px 6px;
  background: none;
  border: none;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
  line-height: 1.2;
  white-space: normal;
  overflow-wrap: anywhere;
  min-height: 42px;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.2s;
}

.tab.active {
  color: var(--color-primary-light);
  border-bottom-color: var(--color-primary);
}

.tab:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.tab-content {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.sidebar-foot {
  border-top: 1px solid var(--color-border);
  padding: 10px 12px;
  background: rgba(0, 0, 0, 0.18);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.sidebar-version {
  color: var(--color-text-secondary);
  font-size: var(--font-xs);
  line-height: 1.35;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.locale-switcher {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
}
.locale-switcher__label {
  white-space: nowrap;
}
.locale-switcher__select {
  flex: 1;
  min-width: 0;
  font-size: var(--font-sm);
  padding: 4px 8px;
}

/* Character list */
.character-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.character-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid transparent;
  transition: all 0.2s;
}

.character-card:hover {
  background: rgba(255, 255, 255, 0.06);
}

.character-card.selected {
  background: rgba(183, 93, 63, 0.15);
  border-color: var(--color-primary);
}

.card-avatar {
  position: relative;
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--color-surface);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
  font-size: 16px;
  color: var(--color-primary-light);
  flex-shrink: 0;
  overflow: visible;
}

.card-avatar-img {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  object-fit: cover;
  display: block;
}

.card-avatar-letter {
  display: block;
}

.card-unread-badge {
  position: absolute;
  top: -4px;
  right: -4px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: #e74c3c;
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
  box-shadow: 0 0 0 2px var(--color-bg, #1a1a1a);
  pointer-events: none;
  letter-spacing: 0;
}

.card-info {
  flex: 1;
  min-width: 0;
}

.card-delete {
  opacity: 0;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: rgba(255, 255, 255, 0.08);
  border: none;
  color: var(--color-text-secondary);
  font-size: 18px;
  line-height: 1;
  cursor: pointer;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.2s, color 0.2s, opacity 0.2s;
}

.character-card:hover .card-delete,
.character-card.selected .card-delete {
  opacity: 1;
}

@media (hover: none) {
  .card-delete {
    opacity: 0.7;
  }
}

.card-delete:hover {
  background: rgba(231, 76, 60, 0.25);
  color: #ff8a75;
}

.card-name {
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-meta {
  display: flex;
  min-width: 0;
  margin-top: 2px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.btn-new-char {
  width: 100%;
  padding: 10px;
  background: rgba(255, 255, 255, 0.04);
  border: 1px dashed var(--color-border);
  border-radius: 8px;
  color: var(--color-text-secondary);
  font-size: 13px;
  transition: all 0.2s;
}

.btn-new-char:hover {
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text);
}

/* 回憶錄入口卡片：玩家側專屬導引 */
.memoir-entry {
  display: flex;
}
.memoir-link {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  padding: 10px 12px;
  background: linear-gradient(135deg, rgba(183, 93, 63, 0.18), rgba(107, 153, 178, 0.18));
  border: 1px solid rgba(183, 93, 63, 0.32);
  border-radius: 8px;
  text-decoration: none;
  color: var(--color-text);
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: transform 0.15s, border-color 0.15s;
}
.memoir-link:hover {
  transform: translateY(-1px);
  border-color: var(--color-primary-light);
}
.memoir-icon {
  font-size: 18px;
}
.memoir-text {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-width: 0;
}
.memoir-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary-light);
}
.memoir-sub {
  font-size: 11px;
  color: var(--color-text-secondary);
}
.memoir-arrow {
  color: var(--color-text-secondary);
  font-size: 14px;
}

/* 通用 admin 連結 hint */
.admin-link-hint {
  margin: 0;
  padding: 8px 10px;
  font-size: 11px;
  color: var(--color-text-secondary);
  background: rgba(255, 255, 255, 0.03);
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  line-height: 1.6;
}

.admin-link-hint__link {
  color: var(--color-primary);
  text-decoration: none;
}

.admin-link-hint__link:hover {
  text-decoration: underline;
}

.reset-feedback {
  font-size: 11px;
  color: #7dc49a;
  margin-top: 4px;
}
</style>
