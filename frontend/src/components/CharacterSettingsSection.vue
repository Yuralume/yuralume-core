<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Character } from '@/types/character'
import type { MessagingPlatform } from '@/types/messaging'
import ChannelBindingsPanel from './ChannelBindingsPanel.vue'
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

  <section class="voice-pregen-section">
    <ProactiveMessageSetting
      :character="character"
      @updated="emit('updated', $event)"
    />
  </section>

  <div class="voice-section">
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

.channel-settings-anchor {
  scroll-margin-top: 12px;
}
</style>
