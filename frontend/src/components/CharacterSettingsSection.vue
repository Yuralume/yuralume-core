<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type { MessagingPlatform } from '@/types/messaging'
import { UiBadge } from '@/components/ui'
import ChannelBindingsPanel from './ChannelBindingsPanel.vue'
import CharacterBackupPanel from './CharacterBackupPanel.vue'
import CharacterEditPanel from './CharacterEditPanel.vue'
import CollapsibleSection from './CollapsibleSection.vue'
import ProactiveMessageSetting from './ProactiveMessageSetting.vue'
import RelationshipNamesEditor from './RelationshipNamesEditor.vue'
import SimpleVoicePicker from './SimpleVoicePicker.vue'
import DispositionAdminEditor from './admin/DispositionAdminEditor.vue'

const props = defineProps<{
  character: Character
  characters: Character[]
  channelSetupPlatform: MessagingPlatform | null
  channelSetupSignal: number
}>()

const emit = defineEmits<{
  updated: [char: Character]
  dataReset: [char: Character]
}>()

const { t } = useI18n()
const channelSettingsAnchor = ref<HTMLElement | null>(null)

async function scrollToChannels() {
  await nextTick()
  channelSettingsAnchor.value?.scrollIntoView({
    behavior: 'smooth',
    block: 'start',
  })
}

watch(() => props.channelSetupSignal, (signal, previous) => {
  if (signal === previous) return
  void scrollToChannels()
})

defineExpose({
  scrollToChannels,
})
</script>

<template>
  <section class="character-settings-header">
    <h3 class="character-settings-header__title">
      {{ t('playerSidebar.settings.characterScopeTitle', { name: character.name }) }}
    </h3>
  </section>

  <CharacterEditPanel
    :key="character.id"
    :character="character"
    :characters="characters"
    :show-tool-settings="false"
    :show-state-settings="false"
    :show-admin-links="false"
    :show-image-trigger-info="false"
    :show-technical-hints="false"
    @updated="emit('updated', $event)"
    @data-reset="emit('dataReset', $event)"
  />

  <CollapsibleSection
    :title="t('playerSidebar.relationshipNames.title')"
    :hint="t('playerSidebar.relationshipNames.sectionHint')"
    :default-open="false"
  >
    <RelationshipNamesEditor :key="`${character.id}:rel-names`" :character="character" />
  </CollapsibleSection>

  <CollapsibleSection
    :title="t('playerSidebar.characters.dispositionSectionTitle')"
    :hint="t('playerSidebar.characters.dispositionSectionHint')"
    :default-open="false"
  >
    <DispositionAdminEditor
      :key="`${character.id}:player-disposition`"
      :character="character"
      :patch="(updated) => emit('updated', updated)"
      surface="player"
    />
  </CollapsibleSection>

  <CollapsibleSection
    :title="t('characterBackup.sectionTitle')"
    :hint="t('characterBackup.sectionHint')"
    :default-open="false"
  >
    <!-- EC3：託管角色的完整人設只存在伺服器側，`.lumebackup` 是整份資料
         匯出，伺服端已整份拒絕託管角色的匯出請求——這裡先一步不給誤按
         的機會，同一備份頁下其他一般角色不受影響。 -->
    <div v-if="character.managed" class="managed-backup-notice">
      <UiBadge variant="primary">{{ t('characterEdit.managed.backupBadge') }}</UiBadge>
      <p class="managed-backup-notice__text">{{ t('characterEdit.managed.backupNotice') }}</p>
    </div>
    <CharacterBackupPanel
      v-else
      :key="`${character.id}:backup`"
      :character="character"
    />
  </CollapsibleSection>

  <section class="voice-pregen-section">
    <ProactiveMessageSetting
      :character="character"
      @updated="emit('updated', $event)"
    />
  </section>

  <!-- EC2-B：託管角色的專屬聲音由授權方鎖定，PATCH voice_profile 會被
       伺服端拒絕，前端整段隱藏 picker 而非灰掉。 -->
  <div v-if="character.managed" class="voice-section managed-voice-notice">
    <UiBadge variant="primary">{{ t('characterEdit.managed.voiceBadge') }}</UiBadge>
    <p class="managed-voice-notice__text">{{ t('characterEdit.managed.voiceNotice') }}</p>
  </div>
  <div v-else class="voice-section">
    <SimpleVoicePicker
      :character="character"
      @updated="emit('updated', $event)"
    />
  </div>

  <div
    id="player-channel-settings"
    ref="channelSettingsAnchor"
    class="channel-settings-anchor"
  >
    <CollapsibleSection
      :title="t('playerSidebar.settings.channelsSectionTitle')"
      :default-open="false"
      :open-signal="channelSetupSignal"
    >
      <ChannelBindingsPanel
        :character-id="character.id"
        :initial-platform="channelSetupPlatform"
        :open-create-signal="channelSetupSignal"
      />
    </CollapsibleSection>
  </div>
</template>

<style scoped>
.character-settings-header {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.character-settings-header__title {
  margin: 0;
  color: var(--color-primary-light);
  font-size: var(--font-xs);
  font-weight: 700;
}

.voice-section,
.voice-pregen-section {
  padding-top: var(--space-3);
  border-top: 1px solid var(--color-border);
}

.managed-voice-notice {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.managed-voice-notice__text {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.managed-backup-notice {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
}

.managed-backup-notice__text {
  margin: 0;
  font-size: var(--font-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.channel-settings-anchor {
  scroll-margin-top: 12px;
}
</style>
