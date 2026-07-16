<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type { MessagingPlatform } from '@/types/messaging'
import {
  isCharacterScopeAvailable,
  resolveSettingsScope,
  type SettingsScope,
} from '@/utils/settingsScope'
import CharacterSettingsSection from './CharacterSettingsSection.vue'
import PersonalSettingsSection from './PersonalSettingsSection.vue'
import PlayerEmptyState from './PlayerEmptyState.vue'

const props = defineProps<{
  characters: Character[]
  selectedCharacter: Character | null
}>()

const emit = defineEmits<{
  characterUpdated: [char: Character]
  characterDataReset: [char: Character]
  createCharacter: []
}>()

const { t } = useI18n()
const settingsScope = ref<SettingsScope>('personal')
const characterSettingsSection = ref<InstanceType<typeof CharacterSettingsSection> | null>(null)
const personalSettingsSection = ref<InstanceType<typeof PersonalSettingsSection> | null>(null)
const channelSetupPlatform = ref<MessagingPlatform | null>(null)
const channelSetupSignal = ref(0)

const characterScopeAvailable = computed(() => (
  isCharacterScopeAvailable(Boolean(props.selectedCharacter))
))

watch(() => props.selectedCharacter?.id ?? null, () => {
  settingsScope.value = resolveSettingsScope({
    current: settingsScope.value,
    hasCharacter: characterScopeAvailable.value,
  })
})

function selectScope(scope: SettingsScope) {
  settingsScope.value = scope
}

async function openChannelSetup(platform: MessagingPlatform) {
  channelSetupPlatform.value = platform
  channelSetupSignal.value += 1
  settingsScope.value = 'character'
  await nextTick()
  await characterSettingsSection.value?.scrollToChannels()
}

// 玩家略過綁定通道後，切到個人設定並閃一下系統推播提醒。
async function highlightWebNotification() {
  settingsScope.value = 'personal'
  await nextTick()
  await personalSettingsSection.value?.flashWebNotification()
}

// provider 引導第一階段：切到個人設定並閃一下「管理者設定」後台入口。
async function highlightAdminEntry() {
  settingsScope.value = 'personal'
  await nextTick()
  await personalSettingsSection.value?.flashAdminEntry()
}

defineExpose({
  openChannelSetup,
  highlightWebNotification,
  highlightAdminEntry,
})
</script>

<template>
  <div class="settings-scope-tabs" role="tablist" :aria-label="t('playerSidebar.settings.scope.ariaLabel')">
    <button
      type="button"
      :class="['settings-scope-tab', { active: settingsScope === 'personal' }]"
      role="tab"
      :aria-selected="settingsScope === 'personal'"
      @click="selectScope('personal')"
    >
      {{ t('playerSidebar.settings.scope.personal') }}
    </button>
    <button
      type="button"
      :class="['settings-scope-tab', {
        active: settingsScope === 'character',
        unavailable: !characterScopeAvailable,
      }]"
      role="tab"
      :aria-disabled="!characterScopeAvailable"
      :aria-selected="settingsScope === 'character'"
      @click="selectScope('character')"
    >
      {{ t('playerSidebar.settings.scope.character') }}
    </button>
  </div>

  <PersonalSettingsSection
    v-if="settingsScope === 'personal'"
    ref="personalSettingsSection"
  />

  <template v-else>
    <CharacterSettingsSection
      v-if="selectedCharacter"
      ref="characterSettingsSection"
      :character="selectedCharacter"
      :characters="characters"
      :channel-setup-platform="channelSetupPlatform"
      :channel-setup-signal="channelSetupSignal"
      @updated="emit('characterUpdated', $event)"
      @data-reset="emit('characterDataReset', $event)"
    />
    <PlayerEmptyState
      v-else
      icon="⚙"
      :title="t('playerSidebar.emptyStates.settingsTitle')"
      :hint="t('playerSidebar.emptyStates.settingsHint')"
      :action-label="t('playerSidebar.actions.newCharacter')"
      @action="emit('createCharacter')"
    />
  </template>
</template>

<style scoped>
.settings-scope-tabs {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  padding: 2px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.03);
  gap: 2px;
}

.settings-scope-tab {
  min-width: 0;
  min-height: 34px;
  padding: 7px 8px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--font-sm);
  font-weight: 600;
  line-height: 1.2;
  cursor: pointer;
  transition: background 0.16s, color 0.16s;
}

.settings-scope-tab.active {
  background: rgba(183, 93, 63, 0.18);
  color: var(--color-primary-light);
}

.settings-scope-tab.unavailable:not(.active) {
  opacity: 0.45;
}
</style>
